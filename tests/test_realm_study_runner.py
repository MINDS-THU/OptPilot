from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import optpilot.realm_study_runner as realm_study_runner
import optpilot.realm_run_execution_service as run_execution_service
from optpilot.realm.errors import RealmConflict
from optpilot.realm.local_runtime import LocalRealmRuntime
from optpilot.realm.run_projection import RunSummaryProjection
from optpilot.realm_retained_batch_run_driver import (
    RealmRetainedBatchRunDriver as _RealRetainedBatchRunDriver,
)
from optpilot.realm_study_runner import local_study_run_id_for_operation
from optpilot.retained_batch_runtime import RetainedBatchRuntimeProvider
from optpilot.run_execution_profile import RunExecutionProfile
from tests.test_retained_study_service import _write_package


class _ProjectionOnlyDriver:
    calls: list[dict[str, object]] = []

    def __init__(self, runtime: LocalRealmRuntime, authority) -> None:
        self.runtime = runtime
        self.authority = authority
        self.run_id = authority.run_id

    @classmethod
    def take_over(
        cls,
        runtime: LocalRealmRuntime,
        *,
        expected_controller,
        takeover_operation_id,
        new_controller_holder_id,
        candidate_normalizer,
        normalizer_version: str,
        **kwargs,
    ) -> "_ProjectionOnlyDriver":
        normalized = candidate_normalizer(
            {"candidate_id": "candidate-a", "spec": {"x": 0.5}}
        )
        # Retained contracts are deeply frozen.  The public normalizer must
        # still return an ordinary, defensively copyable JSON candidate.
        normalized = copy.deepcopy(normalized)
        cls.calls.append(
            {
                "run_id": expected_controller.run_id,
                "normalizer_version": normalizer_version,
                "normalized": normalized,
                "kwargs": kwargs,
            }
        )
        real = _RealRetainedBatchRunDriver.take_over(
            runtime,
            expected_controller=expected_controller,
            takeover_operation_id=takeover_operation_id,
            new_controller_holder_id=new_controller_holder_id,
            candidate_normalizer=candidate_normalizer,
            normalizer_version=normalizer_version,
            **kwargs,
        )
        return cls(runtime, real.authority)

    def run(self) -> RunSummaryProjection:
        snapshot = self.runtime.ledger.read_run_snapshot(
            actor_principal_id=self.runtime.actor_principal_id,
            run_id=self.run_id,
        )
        return RunSummaryProjection.from_snapshot(snapshot)


@unittest.skipUnless(os.name == "posix", "local Realm runtime is POSIX-only")
class RealmStudyRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.study_path = _write_package(self.package_root)
        self.runtime = LocalRealmRuntime.open(
            realm_root=self.root / "realm",
            actor_principal_id="operator",
        )
        self.addCleanup(self.runtime.close)
        _ProjectionOnlyDriver.calls = []

    def launch(self, operation_id: str) -> RunSummaryProjection:
        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            return realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
            )

    def test_composes_one_path_free_realm_run_and_contract_normalizer(self) -> None:
        summary = self.launch("realm-study-run/one")

        self.assertIsInstance(summary, RunSummaryProjection)
        self.assertEqual(summary.run_status, "running")
        self.assertNotIn(str(self.root), repr(summary.to_dict()))
        self.assertEqual(len(_ProjectionOnlyDriver.calls), 1)
        call = _ProjectionOnlyDriver.calls[0]
        self.assertEqual(
            call["normalizer_version"], "optpilot.candidate-normalizer.v1"
        )
        normalized = call["normalized"]
        self.assertEqual(normalized["candidate_id"], "candidate-a")
        self.assertEqual(normalized["format"], "parameters")
        self.assertEqual(
            normalized["generator"]["method_id"], "retained-local-method"
        )
        self.assertFalse((self.package_root / "runs").exists())

    def test_same_operation_replays_the_same_definition_and_run(self) -> None:
        first = self.launch("realm-study-run/replay")
        second = self.launch("realm-study-run/replay")

        self.assertEqual(second.run_id, first.run_id)
        page = self.runtime.run_reader.list_runs(limit=10)
        self.assertEqual([item.run_id for item in page.items], [first.run_id])
        self.assertEqual(len(_ProjectionOnlyDriver.calls), 1)

    def test_direct_environment_binding_is_stable_for_process_lifetime_replay(
        self,
    ) -> None:
        method_path = self.package_root / "configs" / "methods" / "method.yaml"
        method_path.write_text(
            method_path.read_text(encoding="utf-8")
            + "runtime:\n"
            + "  sandbox: process\n"
            + "  envFromHost: [TEST_METHOD_TOKEN]\n",
            encoding="utf-8",
        )
        operation_id = "realm-study-run/process-environment-replay"

        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            first = realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                method_environment={"TEST_METHOD_TOKEN": "private-test-value"},
            )
            replay = realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                method_environment={"TEST_METHOD_TOKEN": "private-test-value"},
            )

        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(len(_ProjectionOnlyDriver.calls), 1)
        launch = self.runtime.study_launches.read_for_run(run_id=first.run_id)
        binding = launch.job.plan.to_dict()["input_facts"][
            "method_environment_binding"
        ]
        self.assertEqual(binding["recoverability"], "process-lifetime")
        self.assertEqual(
            binding["requirements"],
            [
                {
                    "name": "TEST_METHOD_TOKEN",
                    "revision_id": binding["binding_revision"],
                    "source": "process-environment",
                }
            ],
        )
        self.assertNotIn("private-test-value", repr(launch.to_dict()))
        with self.assertRaisesRegex(RealmConflict, "Method environment binding"):
            realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                method_environment={"TEST_METHOD_TOKEN": "changed-private-value"},
            )

    def test_custom_execution_profile_is_retained_replayed_and_enforced(self) -> None:
        operation_id = "realm-study-run/custom-execution-profile"
        expected = RunExecutionProfile(
            controller_ttl_seconds=37,
            heartbeat_interval_seconds=7,
            attempt_ttl_seconds=41,
            method_start_timeout_seconds=13,
            method_request_timeout_seconds=17,
        )

        with mock.patch.object(
            run_execution_service,
            "RealmRetainedBatchRunDriver",
            _ProjectionOnlyDriver,
        ):
            first = realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                controller_ttl_seconds=37,
                heartbeat_interval_seconds=7,
                attempt_ttl_seconds=41,
                method_start_timeout=13,
                method_request_timeout=17,
            )
            replay = realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                controller_ttl_seconds=37,
                heartbeat_interval_seconds=7,
                attempt_ttl_seconds=41,
                method_start_timeout=13,
                method_request_timeout=17,
            )

        self.assertEqual(replay.run_id, first.run_id)
        self.assertEqual(len(_ProjectionOnlyDriver.calls), 1)
        kwargs = _ProjectionOnlyDriver.calls[0]["kwargs"]
        self.assertEqual(kwargs["controller_ttl_seconds"], 37.0)
        self.assertEqual(kwargs["heartbeat_interval_seconds"], 7.0)
        self.assertEqual(kwargs["attempt_ttl_seconds"], 41.0)
        self.assertEqual(kwargs["method_start_timeout"], 13.0)
        self.assertEqual(kwargs["method_request_timeout"], 17.0)
        launch = self.runtime.study_launches.read_for_run(run_id=first.run_id)
        self.assertEqual(
            dict(launch.job.plan.input_facts["execution_profile"]),
            expected.to_dict(),
        )
        self.assertEqual(launch.to_dict()["execution_profile"], expected.to_dict())

        with self.assertRaisesRegex(RealmConflict, "execution profile"):
            realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id=operation_id,
                controller_ttl_seconds=38,
                heartbeat_interval_seconds=7,
                attempt_ttl_seconds=41,
                method_start_timeout=13,
                method_request_timeout=17,
            )

    def test_execution_profile_rejects_unsafe_or_noncanonical_values(self) -> None:
        for kwargs in (
            {"controller_ttl_seconds": float("nan")},
            {"attempt_ttl_seconds": 86_401},
            {
                "controller_ttl_seconds": 30,
                "heartbeat_interval_seconds": 30,
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunExecutionProfile(**kwargs)
        with self.assertRaisesRegex(ValueError, "floating-point encoding"):
            RunExecutionProfile.from_dict(
                {
                    "attempt_ttl_seconds": 300,
                    "controller_ttl_seconds": 300.0,
                    "heartbeat_interval_seconds": 100.0,
                    "method_request_timeout_seconds": 10.0,
                    "method_start_timeout_seconds": 10.0,
                    "schema": "optpilot.run-execution-profile.v1",
                }
            )

    def test_malformed_candidate_retires_real_method_worker_and_wrapper(self) -> None:
        (self.package_root / "local_package" / "method.py").write_text(
            "class RetainedMethod:\n"
            "    def __init__(self, definition, study_spec, rng):\n"
            "        pass\n"
            "    def propose(self, n_candidates, study_state, evidence_view):\n"
            "        return [{\n"
            "            'candidate_id': 'candidate-malformed',\n"
            "            'format': 'parameters',\n"
            "            'spec': {'x': 0.5},\n"
            "            'unexpected': True,\n"
            "        }]\n"
            "    def observe(self, observations):\n"
            "        pass\n",
            encoding="utf-8",
        )
        handles = []
        original_realize = RetainedBatchRuntimeProvider.realize

        def capture_realize(provider, snapshot, **kwargs):
            handle = original_realize(provider, snapshot, **kwargs)
            handles.append(handle)
            return handle

        with mock.patch.object(
            RetainedBatchRuntimeProvider,
            "realize",
            new=capture_realize,
        ):
            summary = realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=self.study_path,
                operation_id="realm-study-run/malformed-candidate",
            )

        self.assertEqual(summary.run_status, "failed")
        self.assertEqual(summary.stop_code, "protocol_error")
        self.assertEqual(len(handles), 1)
        handle = handles[0]
        reservation = handle._reservation
        proof = self.runtime.process_supervisor.lookup_terminal_proof(
            launch_token=reservation.launch_token,
            binding_id=reservation.binding_id,
            evidence_fingerprint=reservation.evidence_fingerprint,
            launch_request_digest=reservation.launch_request_digest,
        )
        row = self.runtime.process_supervisor._required_row(
            reservation.launch_token
        )
        self.assertTrue(handle.closed)
        self.assertIsNotNone(proof)
        self.assertTrue(row.retired)
        self.assertIsNone(row.request)
        self.assertFalse(
            self.runtime.process_supervisor._launch_directory(
                row.coordinate
            ).exists()
        )

    def test_run_id_can_be_derived_purely_before_launch(self) -> None:
        operation_id = "realm-study-run/predeclared"

        expected = local_study_run_id_for_operation(operation_id)
        summary = self.launch(operation_id)

        self.assertEqual(summary.run_id, expected)
        self.assertEqual(
            local_study_run_id_for_operation(operation_id),
            expected,
        )
        self.assertNotEqual(
            local_study_run_id_for_operation("realm-study-run/another"),
            expected,
        )

    def test_run_id_derivation_rejects_invalid_operation_identity(self) -> None:
        for value in ("", "x" * 513):
            with self.subTest(value_length=len(value)), self.assertRaises(
                (TypeError, ValueError)
            ):
                local_study_run_id_for_operation(value)

    def test_study_must_be_inside_the_explicit_package_root(self) -> None:
        outside = self.root / "outside.yaml"
        outside.write_text("apiVersion: optpilot.io/v1\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "inside the explicit package_root"):
            realm_study_runner.run_local_realm_study(
                runtime=self.runtime,
                package_root=self.package_root,
                study_config_path=outside,
                operation_id="realm-study-run/outside",
            )


if __name__ == "__main__":
    unittest.main()
