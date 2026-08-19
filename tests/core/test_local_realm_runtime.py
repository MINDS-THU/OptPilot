from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    LocalContainerWebProvider,
)
from optpilot.realm.local_runtime import (
    LOCAL_OPERATOR_CAPACITY_POOL,
    LOCAL_REALM_CONTENT_STORE_ID,
    LocalRealmRuntime,
)
from optpilot.realm.operator_capacity_records import OperatorCapacityPoolState
from optpilot.realm.run_reader import LOCAL_REALM_PRINCIPAL_KIND
from tests.core.test_retained_study_service import _write_package
from tests.realm_run_support import TEST_LEASE_TTL_SECONDS


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class LocalRealmRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "realm"

    def open(self) -> LocalRealmRuntime:
        runtime = LocalRealmRuntime.open(
            realm_root=self.root,
            actor_principal_id="local-user:test",
        )
        self.addCleanup(runtime.close)
        return runtime

    def test_components_share_one_ledger_provider_and_store(self) -> None:
        runtime = self.open()

        canonical_root = self.root.resolve()
        self.assertEqual(runtime.root, canonical_root)
        self.assertEqual(runtime.actor_principal_id, "local-user:test")
        self.assertEqual(runtime.principal.kind, LOCAL_REALM_PRINCIPAL_KIND)
        self.assertEqual(runtime.content_store.store_id, LOCAL_REALM_CONTENT_STORE_ID)
        self.assertEqual(runtime.ledger.realm_root, canonical_root / "authority")
        self.assertEqual(runtime.content_store.root, canonical_root / "content")
        self.assertEqual(
            runtime.projection_service.root_binding.path,
            canonical_root / "projections",
        )
        self.assertEqual(
            runtime.editable_workspaces.checkout_root,
            canonical_root / "editable-workspaces",
        )
        self.assertEqual(
            runtime.volume_service.root_binding.path,
            canonical_root / "volumes",
        )
        self.assertEqual(
            runtime.process_supervisor.root,
            canonical_root / "processes",
        )
        self.assertEqual(
            runtime.operator_capacity_pool.pool_name,
            LOCAL_OPERATOR_CAPACITY_POOL,
        )
        self.assertEqual(
            runtime.operator_capacity_pool.state,
            OperatorCapacityPoolState.READY,
        )
        self.assertIsNone(runtime.container_web_provider)
        self.assertIsNone(runtime.container_web_broker_authority)

        self.assertIs(runtime.content_service._ledger, runtime.ledger)
        self.assertIs(
            runtime.content_service._local_stores[LOCAL_REALM_CONTENT_STORE_ID],
            runtime.content_store,
        )
        self.assertIs(runtime.projection_service.ledger, runtime.ledger)
        self.assertIs(
            runtime.projection_service._stores[LOCAL_REALM_CONTENT_STORE_ID],
            runtime.content_store,
        )
        self.assertIs(runtime.editable_workspaces._ledger, runtime.ledger)
        self.assertIs(
            runtime.editable_workspaces._content,
            runtime.content_service,
        )
        self.assertIs(
            runtime.editable_workspaces._projection,
            runtime.projection_service,
        )
        self.assertIs(runtime.volume_service.ledger, runtime.ledger)
        self.assertIs(runtime.execution_binder._ledger, runtime.ledger)
        self.assertIs(
            runtime.execution_binder._projection_service,
            runtime.projection_service,
        )
        self.assertIs(runtime.execution_binder._volume_service, runtime.volume_service)
        self.assertIs(runtime.execution_binder._provider, runtime.process_provider)
        self.assertIs(
            runtime.attempt_launcher._supervisor,
            runtime.process_supervisor,
        )
        self.assertIs(runtime.attempt_finalizer._ledger, runtime.ledger)
        self.assertIs(
            runtime.attempt_finalizer._content_service,
            runtime.content_service,
        )
        self.assertIs(runtime.attempt_provider._ledger, runtime.ledger)
        self.assertIs(runtime.attempt_provider._binder, runtime.execution_binder)
        self.assertIs(runtime.attempt_provider._launcher, runtime.attempt_launcher)
        self.assertIs(runtime.attempt_provider._finalizer, runtime.attempt_finalizer)
        self.assertIs(runtime.retained_study_service._ledger, runtime.ledger)
        self.assertIs(
            runtime.retained_study_service._content_service,
            runtime.content_service,
        )
        self.assertIs(
            runtime.retained_study_service._projection_service,
            runtime.projection_service,
        )
        self.assertIs(
            runtime.retained_study_service._provider,
            runtime.process_provider,
        )
        self.assertIs(runtime.run_reader._ledger, runtime.ledger)
        self.assertEqual(runtime.run_reader.principal_id, runtime.actor_principal_id)
        self.assertIs(runtime.run_views._ledger, runtime.ledger)
        self.assertIs(runtime.child_runs._ledger, runtime.ledger)
        self.assertEqual(
            runtime.child_runs.principal_id,
            runtime.actor_principal_id,
        )
        self.assertIs(runtime.run_execution._runtime, runtime)
        self.assertIs(runtime.selection_actions._ledger, runtime.ledger)
        self.assertEqual(
            runtime.selection_actions.principal_id,
            runtime.actor_principal_id,
        )
        self.assertIs(runtime.selection_content._ledger, runtime.ledger)
        self.assertEqual(
            runtime.selection_content.principal_id,
            runtime.actor_principal_id,
        )
        self.assertIs(
            runtime.selection_content._local_stores[LOCAL_REALM_CONTENT_STORE_ID],
            runtime.content_store,
        )
        runtime.ledger.validate_store_binding(
            store_id=runtime.content_store.store_id,
            backend_kind=runtime.content_store.BACKEND_KIND,
            root_marker=runtime.content_store.root_marker,
        )

    def test_reopen_reuses_principal_and_stable_store_registration(self) -> None:
        first = self.open()
        principal = first.principal
        store_marker = first.content_store.root_marker
        provider = first.process_provider
        first.close()

        second = self.open()
        self.assertEqual(second.principal, principal)
        self.assertEqual(second.content_store.root_marker, store_marker)
        self.assertEqual(second.process_provider, provider)
        second.ledger.validate_store_binding(
            store_id=LOCAL_REALM_CONTENT_STORE_ID,
            backend_kind=second.content_store.BACKEND_KIND,
            root_marker=store_marker,
        )
        with sqlite3.connect(second.ledger.database_path) as connection:
            capacity_reconciliations = connection.execute(
                "SELECT COUNT(*) FROM ledger_transactions "
                "WHERE operation_kind = 'operator-capacity.pool.ensure'"
            ).fetchone()[0]
        self.assertEqual(capacity_reconciliations, 2)

    def test_default_actor_is_bound_from_the_observed_os_user(self) -> None:
        runtime = LocalRealmRuntime.open(realm_root=self.root)
        self.addCleanup(runtime.close)

        self.assertTrue(runtime.actor_principal_id.startswith("local-user:sha256:"))
        self.assertEqual(runtime.principal.principal_id, runtime.actor_principal_id)
        self.assertEqual(runtime.principal.kind, LOCAL_REALM_PRINCIPAL_KIND)

    def test_close_and_context_manager_are_idempotent(self) -> None:
        runtime = self.open()
        with runtime as entered:
            self.assertIs(entered, runtime)
            self.assertFalse(runtime.closed)
        self.assertTrue(runtime.closed)
        runtime.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            runtime.__enter__()
        with self.assertRaisesRegex(RealmIntegrityError, "closed"):
            _ = runtime.ledger.realm_id

    def test_absolute_non_directory_and_symlink_roots_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            LocalRealmRuntime.open(
                realm_root=Path("relative-realm"),
                actor_principal_id="local-user:test",
            )

        file_root = self.base / "file-root"
        file_root.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(RealmIntegrityError):
            LocalRealmRuntime.open(
                realm_root=file_root,
                actor_principal_id="local-user:test",
            )

        target = self.base / "target"
        target.mkdir()
        symlink_root = self.base / "symlink-root"
        symlink_root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(RealmIntegrityError):
            LocalRealmRuntime.open(
                realm_root=symlink_root,
                actor_principal_id="local-user:test",
            )
        self.assertEqual(tuple(target.iterdir()), ())

    def test_container_web_provider_is_an_explicit_trusted_composition(self) -> None:
        image = "example/interface@sha256:" + "a" * 64
        runtime = LocalRealmRuntime.open(
            realm_root=self.root,
            actor_principal_id="local-user:test",
            container_web_executable="docker",
            trusted_container_gateway_images=(ContainerGatewayImageTrust(image),),
        )
        self.addCleanup(runtime.close)

        self.assertIsInstance(runtime.container_web_provider, LocalContainerWebProvider)
        self.assertIsNotNone(runtime.container_web_broker_authority)
        assert runtime.container_web_provider is not None
        self.assertTrue(runtime.container_web_provider.is_gateway_image_trusted(image))
        self.assertEqual(
            runtime.container_web_provider._control_root,
            self.root.resolve() / "container-web",
        )

    def test_container_web_provider_loads_durable_realm_trust_on_reopen(self) -> None:
        image = "example/durable-interface@sha256:" + "c" * 64
        first = self.open()
        first.provider_trust_policy.approve(
            operation_id="local-runtime-test/approve-durable-image",
            image_ref=image,
            reason="Local runtime persistence test.",
        )
        first.close()

        reopened = LocalRealmRuntime.open(
            realm_root=self.root,
            actor_principal_id="local-user:test",
            container_web_executable="docker",
        )
        self.addCleanup(reopened.close)

        assert reopened.container_web_provider is not None
        self.assertEqual(reopened.container_gateway_trust_source, "realm")
        self.assertTrue(
            reopened.container_web_provider.is_gateway_image_trusted(image)
        )

    def test_exact_session_trust_replaces_durable_realm_policy(self) -> None:
        durable_image = "example/durable-interface@sha256:" + "d" * 64
        session_image = "example/session-interface@sha256:" + "e" * 64
        first = self.open()
        first.provider_trust_policy.approve(
            operation_id="local-runtime-test/approve-realm-image",
            image_ref=durable_image,
        )
        first.close()

        session = LocalRealmRuntime.open(
            realm_root=self.root,
            actor_principal_id="local-user:test",
            container_web_executable="docker",
            trusted_container_gateway_images=(
                ContainerGatewayImageTrust(session_image),
            ),
        )
        self.addCleanup(session.close)

        assert session.container_web_provider is not None
        self.assertEqual(session.container_gateway_trust_source, "session")
        self.assertTrue(
            session.container_web_provider.is_gateway_image_trusted(session_image)
        )
        self.assertFalse(
            session.container_web_provider.is_gateway_image_trusted(durable_image)
        )

        disabled = LocalRealmRuntime.open(
            realm_root=self.root,
            actor_principal_id="local-user:test",
            container_web_executable="docker",
            trusted_container_gateway_images=(),
        )
        self.addCleanup(disabled.close)
        assert disabled.container_web_provider is not None
        self.assertEqual(disabled.container_gateway_trust_source, "session")
        self.assertFalse(
            disabled.container_web_provider.is_gateway_image_trusted(durable_image)
        )
        self.assertFalse(
            disabled.container_web_provider.is_gateway_image_trusted(session_image)
        )

    def test_retained_package_prepare_and_run_launch_use_composed_services(
        self,
    ) -> None:
        runtime = self.open()
        package_root = self.base / "package"
        package_root.mkdir()
        study_path = _write_package(package_root)

        preparation = runtime.retained_study_service.prepare_local_package(
            operation_id="local-runtime-test/package",
            actor_principal_id=runtime.actor_principal_id,
            store_id=runtime.content_store.store_id,
            package_root=package_root,
            study_config_path=study_path,
            source_owner_id="local-runtime-test-source",
            study_definition_owner_id="local-runtime-test-definition",
        )
        created = runtime.retained_study_service.launch_definition_run(
            operation_id="local-runtime-test/run",
            actor_principal_id=runtime.actor_principal_id,
            controller_holder_id="local-runtime-test-controller",
            controller_ttl_seconds=TEST_LEASE_TTL_SECONDS,
            preparation=preparation,
            run_id="local-runtime-test-run",
            owner_id="local-runtime-test-run-owner",
        )

        snapshot = runtime.ledger.read_run_snapshot(
            actor_principal_id=runtime.actor_principal_id,
            run_id=created.run.run_id,
        )
        self.assertEqual(snapshot.run.run_id, "local-runtime-test-run")
        self.assertEqual(
            snapshot.definition.digest,
            preparation.study_definition.manifest.run_definition_digest,
        )


if __name__ == "__main__":
    unittest.main()
