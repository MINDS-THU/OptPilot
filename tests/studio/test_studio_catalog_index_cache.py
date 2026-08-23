"""Contracts for Studio's debounced catalog discovery reads.

The catalog index is a discovery projection rebuilt from filesystem scans and
per-head projection leases.  With ``catalog_refresh_ttl_seconds`` set (as the
served UI does), consecutive reads inside the window reuse the last refresh
and the last built index instead of re-walking package sources per request.
Directly constructed states default to a TTL of 0 and keep strictly fresh
reads.  A full (undebounced) refresh — the catalog-publish path — invalidates
the cached index as soon as the head set changes.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from optpilot.realm.content import AllowedTreeSource
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.refs import request_digest
from optpilot_studio.ui import server as studio_server
from optpilot_studio.ui.server import (
    UiState,
    _catalog_index_payload,
    _catalog_payload,
    _refresh_realm_catalog_projections,
)
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


PACKAGE_ARTIFACT_ROLE = "package-plan-artifact"


@unittest.skipUnless(os.name == "posix", "local Realm projections are POSIX-only")
class StudioCatalogIndexCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="local-user:studio-catalog-index-cache-test",
        )
        self.addCleanup(self.runtime.close)
        self.state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
            catalog_refresh_ttl_seconds=300.0,
        )
        self.addCleanup(self.state.close_catalog_projections)
        self.package_id = "index-cache-package"
        self._counter = 0

    def _operation(self, label: str) -> str:
        self._counter += 1
        return f"studio-index-cache/{self._counter}/{label}"

    def _publish(
        self,
        *,
        publisher_id: str,
        files: dict[str, str],
        owned_paths: tuple[str, ...],
    ) -> Any:
        self._counter += 1
        suffix = f"{self._counter}-{uuid.uuid4().hex[:8]}"
        owner_id = f"studio-index-cache-artifact-{suffix}"
        source = self.root / f"source-{suffix}"
        source.mkdir()
        for relative, content in files.items():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        actor = self.runtime.actor_principal_id
        self.runtime.ledger.create_owner(
            operation_id=self._operation(f"owner-{suffix}"),
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
        expected_head = self.runtime.catalog.read_head(package_id=self.package_id)
        identity = {
            "package_id": self.package_id,
            "publisher_id": publisher_id,
            "artifact": str(membership.content_ref),
        }
        return self.runtime.catalog.publish(
            operation_id=self._operation(f"publish-{suffix}"),
            package_id=self.package_id,
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
            expected_head=expected_head,
        )

    def _publish_environment(self) -> None:
        environment = {
            "apiVersion": "optpilot.io/v1",
            "config": "environment",
            "id": "toy",
            "evaluator": {"python": "evaluator:evaluate", "pythonPath": ["."]},
            "candidate": {
                "format": "parameters",
                "parameters": {
                    "schema": {"x": {"valueType": "float", "min": 0, "max": 1}}
                },
            },
            "metrics": {"source": "return", "keys": ["score"]},
        }
        self._publish(
            publisher_id="publisher/toy",
            files={
                "environments/toy/environment.yaml": yaml.safe_dump(
                    environment, sort_keys=False
                ),
                "environments/toy/evaluator.py": (
                    "def evaluate(candidate_runtime, context):\n"
                    "    return {'score': float(candidate_runtime['x'])}\n"
                ),
            },
            owned_paths=("environments/toy",),
        )

    def _advance_catalog_head(self) -> None:
        self._publish(
            publisher_id=f"publisher/marker/{self._counter}",
            files={f"resources/marker-{self._counter}/README.md": "new head\n"},
            owned_paths=(f"resources/marker-{self._counter}",),
        )

    def test_reads_inside_ttl_reuse_refresh_and_scanned_index(self) -> None:
        self._publish_environment()
        first = _catalog_index_payload(self.state)
        self.assertEqual(
            [entry["id"] for entry in first["environments"]], ["toy"]
        )

        with (
            mock.patch.object(
                studio_server,
                "_package_plan_realm_runtime",
                side_effect=AssertionError(
                    "a cached catalog read must not re-list catalog heads"
                ),
            ),
            mock.patch.object(
                studio_server,
                "_scan_catalog",
                side_effect=AssertionError(
                    "a cached catalog read must not re-scan package sources"
                ),
            ),
        ):
            second = _catalog_index_payload(self.state)
            public_first = _catalog_payload(self.state)
            public_second = _catalog_payload(self.state)
        self.assertIs(second, first)
        # Public views deep-copy entries from the shared cached index; one
        # response mutating its copy must never leak into the next response.
        self.assertIsNot(
            public_first["environments"][0], public_second["environments"][0]
        )
        public_first["environments"][0]["id"] = "mutated"
        self.assertEqual(public_second["environments"][0]["id"], "toy")
        self.assertEqual(first["environments"][0]["id"], "toy")

    def test_concurrent_reads_share_one_catalog_build(self) -> None:
        started = threading.Event()
        release = threading.Event()
        payload = {
            "roots": [],
            "environments": [],
            "methods": [],
            "studies": [],
            "resources": [],
            "sources": [],
            "builtins": {},
        }
        builds: list[float] = []

        def build(state, *, ttl_seconds):
            builds.append(ttl_seconds)
            started.set()
            self.assertTrue(release.wait(timeout=5))
            with state._catalog_projection_lock:
                state._catalog_index_cache = (time.monotonic(), payload)
            return payload

        with (
            mock.patch.object(
                studio_server, "_build_catalog_index_payload", side_effect=build
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(_catalog_index_payload, self.state)
            self.assertTrue(started.wait(timeout=5))
            second = pool.submit(_catalog_index_payload, self.state)
            release.set()
            self.assertIs(first.result(timeout=5), payload)
            self.assertIs(second.result(timeout=5), payload)

        self.assertEqual(builds, [300.0])

    def test_zero_ttl_state_keeps_strictly_fresh_reads(self) -> None:
        fresh_state = UiState(
            cwd=self.root / "studio",
            catalog_roots=[],
            run_roots=[],
            realm_runtime=self.runtime,
        )
        self.addCleanup(fresh_state.close_catalog_projections)
        first = _catalog_index_payload(fresh_state)
        self.assertEqual(first["environments"], [])
        self._publish_environment()
        second = _catalog_index_payload(fresh_state)
        self.assertIsNot(second, first)
        self.assertEqual(
            [entry["id"] for entry in second["environments"]], ["toy"]
        )

    def test_full_refresh_after_publish_invalidates_cached_index(self) -> None:
        self._publish_environment()
        first = _catalog_index_payload(self.state)
        self.assertEqual(
            [entry["id"] for entry in first["environments"]], ["toy"]
        )

        # An external head advance is invisible inside the TTL window ...
        self._advance_catalog_head()
        self.assertIs(_catalog_index_payload(self.state), first)

        # ... but Studio's own publish path always runs an undebounced
        # refresh, which drops the cached index once the head set changes.
        _refresh_realm_catalog_projections(self.state)
        refreshed = _catalog_index_payload(self.state)
        self.assertIsNot(refreshed, first)
        current_head = self.runtime.catalog.read_head(package_id=self.package_id)
        assert current_head is not None
        self.assertEqual(current_head.revision, 2)
        self.assertEqual(
            [entry["id"] for entry in refreshed["environments"]], ["toy"]
        )
        # The rebuilt index is cached again for the next window.
        self.assertIs(_catalog_index_payload(self.state), refreshed)


if __name__ == "__main__":
    unittest.main()
