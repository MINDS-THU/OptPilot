from __future__ import annotations

import hashlib
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from optpilot.locked_python_runtime import (
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    LOCKED_PYTHON_RUNTIME_OWNER_KIND,
    LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
    LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
    LockedPythonRuntimeError,
    LockedPythonRuntimePreparer,
)
from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.errors import RealmConflict
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership, OwnerPermission, OwnerState
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.refs import request_digest
from optpilot.realm.service import RealmContentService


_ACTOR = "operator"
_STORE_ID = "local-a"
_CONFIG_PATH = "environments/demo/environment.yaml"


def _zip_entry(name: str, payload: bytes, *, directory: bool = False) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = (
        (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)
    ) << 16
    return entry


def _write_wheel(
    path: Path,
    *,
    purelib: bool = True,
    tag: str = "py3-none-any",
    package_payload: bytes = b"VALUE = 7\n",
    leading_entries: tuple[tuple[str, bytes], ...] = (),
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: optpilot-test\n"
        f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
        f"Tag: {tag}\n"
    ).encode("utf-8")
    entries = (
        *leading_entries,
        ("demo_pkg/__init__.py", package_payload),
        ("demo_pkg-1.0.0.dist-info/METADATA", b"Name: demo-pkg\nVersion: 1.0.0\n"),
        ("demo_pkg-1.0.0.dist-info/WHEEL", wheel_metadata),
        ("demo_pkg-1.0.0.dist-info/RECORD", b""),
        *extra_entries,
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(
                _zip_entry(name, payload, directory=name.endswith("/")), payload
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LockedPythonRuntimePreparerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.component_root = self.package_root / "environments" / "demo"
        self.component_root.mkdir(parents=True)
        (self.component_root / "environment.yaml").write_text(
            "apiVersion: optpilot.io/v1\nconfig: environment\nid: demo\n",
            encoding="utf-8",
        )

        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.addCleanup(self.ledger.close)
        self.ledger.register_principal(
            operation_id="principal/register",
            principal_id=_ACTOR,
            kind="human",
        )
        self.store = LocalContentStore(self.root / "store", store_id=_STORE_ID)
        self.addCleanup(self.store.close)
        self.ledger.register_store(
            operation_id="store/register",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content = RealmContentService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
        )
        self.provider = ProcessProviderIdentity(
            builder_fingerprint="a" * 64,
            platform="test-platform",
        )
        self.preparer = LockedPythonRuntimePreparer(
            self.ledger,
            self.content,
            actor_principal_id=_ACTOR,
            store_id=_STORE_ID,
            provider=self.provider,
            cache_root=self.root / "runtime-cache",
        )

    def _write_lock(
        self,
        *,
        wheel_name: str = "demo_pkg-1.0.0-py3-none-any.whl",
        purelib: bool = True,
        tag: str = "py3-none-any",
        package_payload: bytes = b"VALUE = 7\n",
        leading_entries: tuple[tuple[str, bytes], ...] = (),
        extra_entries: tuple[tuple[str, bytes], ...] = (),
        locked_path: str | None = None,
        locked_digest: str | None = None,
    ) -> Path:
        wheel = self.component_root / "vendor" / wheel_name
        digest = _write_wheel(
            wheel,
            purelib=purelib,
            tag=tag,
            package_payload=package_payload,
            leading_entries=leading_entries,
            extra_entries=extra_entries,
        )
        (self.component_root / "requirements.lock").write_text(
            f"{locked_path or 'vendor/' + wheel_name} "
            f"--hash=sha256:{locked_digest or digest}\n",
            encoding="utf-8",
        )
        return wheel

    def _retain_package(self):
        self.ledger.create_owner(
            operation_id="package/create",
            owner_id="package-source",
            owner_kind="workspace",
            principal_id=_ACTOR,
        )
        change = self.ledger.begin_owner_change(
            operation_id="package/begin",
            actor_principal_id=_ACTOR,
            owner_id="package-source",
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        seal = self.content.capture(
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            store_id=_STORE_ID,
        ).seal_tree(
            source=AllowedTreeSource(self.package_root),
            operation_id="package/seal",
        )
        membership = OwnerMembership(_STORE_ID, seal.snapshot_ref, "package-source")
        self.ledger.hold_owner_content(
            operation_id="package/hold",
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            memberships=(membership,),
        )
        self.ledger.commit_owner_change(
            operation_id="package/commit",
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(membership,),
        )
        return seal.snapshot_ref

    @staticmethod
    def _runtime(*, timeout_seconds: int = 60) -> dict[str, object]:
        return {
            "setup": {
                "cache": "prepared",
                "timeoutSeconds": timeout_seconds,
                "steps": [
                    {
                        "uses": "python-venv",
                        "cwd": ".",
                        "requirements": ["requirements.lock"],
                    }
                ],
            }
        }

    def _prepare(self, *, operation_id: str = "prepare/one"):
        return self.preparer.prepare(
            operation_id=operation_id,
            package_root=self.package_root,
            package_snapshot=self.package_snapshot,
            component_kind="environment",
            component_id="demo",
            config_relative_path=_CONFIG_PATH,
            runtime=self._runtime(),
        )

    def _runtime_owner_count(self) -> int:
        with sqlite3.connect(self.ledger.database_path) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM owners WHERE owner_kind = ?",
                    (LOCKED_PYTHON_RUNTIME_OWNER_KIND,),
                ).fetchone()[0]
            )

    @staticmethod
    def _runtime_owner_id(cache_key: str) -> str:
        return "locked-python-runtime-" + request_digest(
            {
                "actor_principal_id": _ACTOR,
                "cache_key": cache_key,
                "schema": LOCKED_PYTHON_RUNTIME_POLICY_SCHEMA,
            }
        )

    def test_prepare_builds_once_and_retains_exact_cached_tree(self) -> None:
        self._write_lock()
        self.package_snapshot = self._retain_package()

        prepared = self._prepare()

        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertEqual(prepared.component_kind, "environment")
        self.assertEqual(prepared.scope, ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        self.assertEqual(prepared.import_roots, (".",))
        self.assertEqual(prepared.membership.store_id, _STORE_ID)
        self.assertEqual(
            prepared.membership.role,
            LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
        )
        self.assertEqual(prepared.source_anchor.owner_revision, 1)
        self.assertTrue(
            prepared.source_anchor.owner_id.startswith("locked-python-runtime-")
        )

        owner = self.ledger.read_owner(
            actor_principal_id=_ACTOR,
            owner_id=prepared.source_anchor.owner_id,
            permission=OwnerPermission.DERIVE,
        )
        self.assertEqual(owner.owner_kind, LOCKED_PYTHON_RUNTIME_OWNER_KIND)
        self.assertEqual(owner.state, OwnerState.ACTIVE)
        self.assertEqual(owner.revision, 1)
        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id=_ACTOR,
                owner_id=owner.owner_id,
            ),
            (prepared.membership,),
        )

        manifest = self.store.verify_tree(prepared.membership.content_ref)
        paths = {entry.path for entry in manifest.entries}
        self.assertIn("prepared-runtime.json", paths)
        self.assertIn("site-packages/demo_pkg/__init__.py", paths)
        self.assertIn("site-packages/demo_pkg-1.0.0.dist-info/WHEEL", paths)
        self.assertFalse(any(path.endswith(".whl") for path in paths))

        with mock.patch.object(
            self.preparer,
            "_build",
            side_effect=AssertionError("cache hit must not rebuild"),
        ):
            replay = self._prepare(operation_id="prepare/two")
        self.assertEqual(replay, prepared)

    def test_hash_mismatch_is_rejected_before_cache_or_realm_mutation(self) -> None:
        self._write_lock(locked_digest="0" * 64)
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(raised.exception.code, "dependency_hash_mismatch")
        self.assertEqual(list(self.preparer.cache.entries_root.iterdir()), [])
        self.assertEqual(self._runtime_owner_count(), 0)

    def test_lock_path_cannot_escape_registered_package(self) -> None:
        wheel = self._write_lock()
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (self.component_root / "requirements.lock").write_text(
            f"../../../outside.whl --hash=sha256:{digest}\n",
            encoding="utf-8",
        )
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(raised.exception.code, "dependency_path_invalid")

    def test_wheel_member_path_traversal_is_rejected(self) -> None:
        self._write_lock(extra_entries=(("../escape.py", b"escaped = True\n"),))
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(raised.exception.code, "dependency_wheel_path_invalid")
        self.assertFalse((self.root / "escape.py").exists())

    def test_platform_wheel_is_rejected_even_when_metadata_claims_purelib(self) -> None:
        self._write_lock(
            wheel_name="demo_pkg-1.0.0-cp313-cp313-manylinux_2_28_x86_64.whl",
            purelib=True,
            tag="cp313-cp313-manylinux_2_28_x86_64",
        )
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(
            raised.exception.code,
            "dependency_wheel_platform_unsupported",
        )

    def test_casefolding_installed_path_collision_is_rejected(self) -> None:
        self._write_lock(
            extra_entries=(
                ("demo_pkg/Feature.py", b"FEATURE = 1\n"),
                ("demo_pkg/feature.py", b"FEATURE = 2\n"),
            )
        )
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(raised.exception.code, "dependency_wheel_collision")

    def test_wheel_mutation_between_plan_and_build_is_rejected_without_retention(
        self,
    ) -> None:
        wheel = self._write_lock()
        self.package_snapshot = self._retain_package()
        real_acquire = self.preparer.cache.acquire

        def mutate_then_acquire(**kwargs):
            _write_wheel(wheel, package_payload=b"MUTATED = True\n")
            return real_acquire(**kwargs)

        with mock.patch.object(
            self.preparer.cache,
            "acquire",
            side_effect=mutate_then_acquire,
        ):
            with self.assertRaises(LockedPythonRuntimeError) as raised:
                self._prepare()

        self.assertEqual(raised.exception.code, "dependency_source_changed")
        self.assertEqual(list(self.preparer.cache.entries_root.iterdir()), [])
        self.assertEqual(self._runtime_owner_count(), 0)

    def test_prepopulated_runtime_owner_with_wrong_tree_is_rejected(self) -> None:
        self._write_lock()
        self.package_snapshot = self._retain_package()
        runtime = self._runtime()
        plan = self.preparer.plan(
            package_root=self.package_root,
            package_snapshot=self.package_snapshot,
            component_kind="environment",
            component_id="demo",
            config_relative_path=_CONFIG_PATH,
            setup=runtime["setup"],
        )
        cache_key = self.preparer.cache.cache_key(plan.key_payload)
        owner_id = self._runtime_owner_id(cache_key)
        self.ledger.create_owner(
            operation_id="wrong-runtime/create",
            owner_id=owner_id,
            owner_kind=LOCKED_PYTHON_RUNTIME_OWNER_KIND,
            principal_id=_ACTOR,
        )
        wrong_root = self.root / "wrong-runtime"
        wrong_root.mkdir()
        (wrong_root / "unrelated.py").write_text(
            "THIS_IS_NOT_THE_LOCKED_RUNTIME = True\n",
            encoding="utf-8",
        )
        change = self.ledger.begin_owner_change(
            operation_id="wrong-runtime/begin",
            actor_principal_id=_ACTOR,
            owner_id=owner_id,
            expected_owner_revision=0,
            ttl_seconds=60,
        )
        wrong_seal = self.content.capture(
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            store_id=_STORE_ID,
        ).seal_tree(
            source=AllowedTreeSource(wrong_root),
            operation_id="wrong-runtime/seal",
        )
        wrong_membership = OwnerMembership(
            _STORE_ID,
            wrong_seal.snapshot_ref,
            LOCKED_PYTHON_RUNTIME_SOURCE_ROLE,
        )
        self.ledger.hold_owner_content(
            operation_id="wrong-runtime/hold",
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            memberships=(wrong_membership,),
        )
        self.ledger.commit_owner_change(
            operation_id="wrong-runtime/commit",
            actor_principal_id=_ACTOR,
            change_id=change.change_id,
            expected_owner_revision=0,
            additions=(wrong_membership,),
        )

        with self.assertRaises(RealmConflict):
            self._prepare()

        self.assertEqual(
            self.ledger.list_owner_memberships(
                actor_principal_id=_ACTOR,
                owner_id=owner_id,
            ),
            (wrong_membership,),
        )

    def test_explicit_directory_and_file_collision_has_stable_rejection(self) -> None:
        self._write_lock(
            extra_entries=(
                ("demo_pkg/collision/", b""),
                ("demo_pkg/collision", b"not a directory\n"),
            )
        )
        self.package_snapshot = self._retain_package()

        with self.assertRaises(LockedPythonRuntimeError) as raised:
            self._prepare()

        self.assertEqual(raised.exception.code, "dependency_wheel_collision")

    def test_deadline_is_enforced_while_extracting_one_large_entry(self) -> None:
        large_payload = b"A" * (3 * 1024 * 1024)
        self._write_lock(
            leading_entries=(("demo_pkg/large-payload.bin", large_payload),)
        )
        self.package_snapshot = self._retain_package()
        runtime = self._runtime(timeout_seconds=1)
        plan = self.preparer.plan(
            package_root=self.package_root,
            package_snapshot=self.package_snapshot,
            component_kind="environment",
            component_id="demo",
            config_relative_path=_CONFIG_PATH,
            setup=runtime["setup"],
        )
        payload_root = self.root / "deadline-payload"
        payload_root.mkdir()
        monotonic_calls = 0

        def advancing_monotonic() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            return 2.0 if monotonic_calls >= 7 else 0.0

        with mock.patch(
            "optpilot.locked_python_runtime.time.monotonic",
            side_effect=advancing_monotonic,
        ):
            with self.assertRaises(LockedPythonRuntimeError) as raised:
                self.preparer._build(plan, payload_root)

        self.assertEqual(raised.exception.code, "dependency_preparation_timeout")
        self.assertGreaterEqual(monotonic_calls, 7)
        self.assertFalse((payload_root / "prepared-runtime.json").exists())
        self.assertEqual(self._runtime_owner_count(), 0)


if __name__ == "__main__":
    unittest.main()
