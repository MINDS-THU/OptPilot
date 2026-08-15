from __future__ import annotations

from types import SimpleNamespace
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from optpilot.attempts import AttemptEnvelope, EvaluationSpec
from optpilot.realm.content import LocalContentStore
from optpilot.realm.ephemeral_volume_service import RealmEphemeralVolumeService
from optpilot.realm.errors import RealmConflict, RealmIntegrityError
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.local_attempt_launcher import (
    LocalAttemptExecutionBinding,
    LocalAttemptPlatformError,
    RealmLocalAttemptLauncher,
)
from optpilot.realm.local_attempt_protocol import (
    ATTEMPT_REQUEST_FILE,
    ATTEMPT_RESULT_FILE,
    MAX_LOCAL_ATTEMPT_LOG_BYTES,
    LocalAttemptWorkerLog,
    LocalAttemptWorkerRequest,
    LocalAttemptWorkerResult,
    require_host_paths_absent,
)
from optpilot.realm.local_process_supervisor import (
    LocalProcessSupervisor,
    WorkerStarted,
    WorkerTerminalProof,
)
from optpilot.realm.process_execution_binder import RealmProcessExecutionBinder
from optpilot.realm.process_provider import ProcessProviderIdentity
from optpilot.realm.projection_service import RealmProjectionService
from optpilot.realm.refs import canonical_json_bytes
from optpilot.realm.refs import request_digest
from optpilot.realm.run_closure import RUN_ATTEMPT_INPUT_ROLE, ScopePath
from optpilot.realm.run_records import (
    CandidateAdmission,
    LogicalTrialAdmission,
    NormalizedCandidateEnvelope,
    RunAdmissionPlan,
)
from optpilot.realm.service import RealmContentService
from optpilot.retained_study_service import RetainedStudyService
from optpilot.run_attempt_heartbeat import RunAttemptHeartbeatCoordinator
from optpilot.runtime_binding import (
    CONTROL_SCOPE,
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    ENVIRONMENT_SOURCE_SCOPE,
    PythonCallableEntrypoint,
    TRIAL_SCOPE,
)
from tests.core.test_retained_study_service import _write_package
from tests.core.test_runtime_binding import (
    _compile as _compile_portable_runtime,
    _definition_with_prepared_python,
)


class _SimulatedParentCrash(BaseException):
    pass


def _evaluation_spec() -> EvaluationSpec:
    return EvaluationSpec(
        environment_id="environment-a",
        environment_revision_digest="a" * 64,
        prepared_runtime_digest="b" * 64,
        candidate_ref="candidate:sha256:" + "c" * 64,
        candidate={
            "candidate_id": "candidate-a",
            "format": "parameters",
            "spec": {"x": 0.5},
            "lineage": {"parents": []},
            "generator": {"method_id": "method-a"},
            "validation": {
                "implementation": "builtin.schema_validation",
                "config": {
                    "enforceBounds": True,
                    "searchSpace": {
                        "x": {"valueType": "float", "min": 0.0, "max": 1.0}
                    },
                    "constraints": [],
                },
            },
            "materialization": {
                "implementation": "builtin.parameter_to_config",
                "config": {},
            },
        },
        objective={"primaryMetric": {"name": "score", "direction": "maximize"}},
        resource_profile={"cpu": 1, "memoryGiB": 1, "timeoutSeconds": 30},
        sandbox_spec={
            "runtimeType": "process",
            "networkPolicy": "disabled",
            "environmentVariables": {},
            "cleanupPolicy": "always",
        },
    )


def _worker_request() -> LocalAttemptWorkerRequest:
    return LocalAttemptWorkerRequest(
        attempt_id="attempt-a",
        binding_id="binding-a",
        launch_token="launch-a",
        evidence_fingerprint="d" * 64,
        evaluation_spec=_evaluation_spec(),
        portable_spec_digest="e" * 64,
        entrypoint=PythonCallableEntrypoint(
            scope=ENVIRONMENT_SOURCE_SCOPE,
            module="local_package.evaluate",
            attribute="evaluate",
        ),
        python_import_roots=(ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),),
        evaluator_settings={"target": 1},
        declared_metric_names=("score",),
    )


def _envelope(request: LocalAttemptWorkerRequest) -> AttemptEnvelope:
    return AttemptEnvelope(
        attempt_id=request.attempt_id,
        evaluation_spec_digest=request.evaluation_spec.digest,
        binding_id=request.binding_id,
        outcome="success",
        phase="environment_evaluation",
        wall_clock_seconds=0.1,
        validation={"accepted": True, "errors": [], "warnings": [], "metadata": {}},
        materialization={"runtime_spec": {"x": 0.5}, "output_files": [], "metadata": {}},
        metric_values={"score": 0.5},
        constraint_results={},
        output_declarations=(),
        event_summary={"primary_metric": "score"},
        execution_metadata={"binding_id": request.binding_id},
        error={},
    )


class LocalAttemptProtocolTest(unittest.TestCase):
    def test_request_accepts_one_final_prepared_environment_import_root(self) -> None:
        request = replace(
            _worker_request(),
            python_import_roots=(
                ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),
                ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "."),
            ),
        )

        self.assertEqual(
            LocalAttemptWorkerRequest.from_dict(request.to_dict()),
            request,
        )

    def test_request_rejects_arbitrary_or_misordered_import_scopes(self) -> None:
        base = _worker_request()
        cases = (
            (
                "arbitrary",
                (
                    ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),
                    ScopePath("untrusted-runtime", "."),
                ),
                "entrypoint source scope or the prepared environment Python scope",
            ),
            (
                "prepared-first",
                (
                    ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "."),
                    ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),
                ),
                "prepared environment Python import root must be last",
            ),
            (
                "prepared-subpath",
                (
                    ScopePath(ENVIRONMENT_SOURCE_SCOPE, "."),
                    ScopePath(ENVIRONMENT_PREPARED_PYTHON_SCOPE, "nested"),
                ),
                "prepared environment Python import root must be last",
            ),
        )
        for name, roots, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                replace(base, python_import_roots=roots)

    def test_request_and_result_are_strict_canonical_path_free_records(self) -> None:
        request = _worker_request()
        result = LocalAttemptWorkerResult(
            request_digest=request.digest,
            attempt_id=request.attempt_id,
            binding_id=request.binding_id,
            launch_token=request.launch_token,
            evidence_fingerprint=request.evidence_fingerprint,
            envelope=_envelope(request),
        )

        self.assertEqual(LocalAttemptWorkerRequest.from_dict(request.to_dict()), request)
        self.assertEqual(LocalAttemptWorkerResult.from_dict(result.to_dict()), result)
        self.assertEqual(
            canonical_json_bytes(json.loads(request.canonical_bytes)),
            request.canonical_bytes,
        )
        self.assertEqual(
            canonical_json_bytes(json.loads(result.canonical_bytes)),
            result.canonical_bytes,
        )
        encoded = request.canonical_bytes + result.canonical_bytes
        self.assertNotIn(b"/private/var/attempt", encoded)
        self.assertNotIn(b"workspace", result.canonical_bytes)

        changed = request.to_dict()
        changed["evaluator_settings"]["target"] = 2
        with self.assertRaisesRegex(ValueError, "digest"):
            LocalAttemptWorkerRequest.from_dict(changed)

        changed_result = result.to_dict()
        changed_result["envelope"]["metric_values"]["score"] = 9
        with self.assertRaisesRegex(ValueError, "digest"):
            LocalAttemptWorkerResult.from_dict(changed_result)

    def test_worker_log_excerpt_is_separate_bounded_and_digest_bound(self) -> None:
        request = _worker_request()
        content = b"line one\nline two\n"
        log = LocalAttemptWorkerLog.build(
            stream="stdout",
            byte_count=len(content) + 100,
            line_count=4,
            content=content,
        )
        result = LocalAttemptWorkerResult(
            request_digest=request.digest,
            attempt_id=request.attempt_id,
            binding_id=request.binding_id,
            launch_token=request.launch_token,
            evidence_fingerprint=request.evidence_fingerprint,
            envelope=_envelope(request),
            logs=(log,),
        )

        self.assertEqual(
            LocalAttemptWorkerResult.from_dict(result.to_dict()), result
        )
        self.assertEqual(result.logs[0].content, content)
        self.assertTrue(result.logs[0].truncated)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            LocalAttemptWorkerLog.build(
                stream="stderr",
                byte_count=MAX_LOCAL_ATTEMPT_LOG_BYTES + 1,
                line_count=0,
                content=b"x" * (MAX_LOCAL_ATTEMPT_LOG_BYTES + 1),
            )
        tampered = result.to_dict()
        tampered["logs"][0]["content_digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            LocalAttemptWorkerResult.from_dict(tampered)

    def test_path_scanner_decodes_log_excerpt_before_portable_publication(self) -> None:
        request = _worker_request()
        host_root = Path("/private/var/attempt-a")
        log = LocalAttemptWorkerLog.build(
            stream="stdout",
            byte_count=len(str(host_root)),
            line_count=1,
            content=str(host_root).encode("utf-8"),
        )
        result = LocalAttemptWorkerResult(
            request_digest=request.digest,
            attempt_id=request.attempt_id,
            binding_id=request.binding_id,
            launch_token=request.launch_token,
            evidence_fingerprint=request.evidence_fingerprint,
            envelope=_envelope(request),
            logs=(log,),
        )

        self.assertNotIn(str(host_root).encode("utf-8"), result.canonical_bytes)
        with self.assertRaisesRegex(RealmIntegrityError, "realized host path"):
            require_host_paths_absent(result.canonical_bytes, (host_root,))

    def test_log_redaction_handles_nested_roots_and_a_path_split_at_the_cap(self) -> None:
        from optpilot.realm._local_attempt_worker import _BoundedTextCapture

        root = Path("/private/runtime")
        nested = root / "source"
        capture = _BoundedTextCapture("stdout")
        first_line = f"nested={nested}/module.py\n"
        capture.write(first_line)
        split_prefix = "x" * (
            MAX_LOCAL_ATTEMPT_LOG_BYTES - len(first_line.encode("utf-8")) - 12
        )
        capture.write(split_prefix + str(nested))

        log = capture.evidence((root, nested))

        self.assertIsNotNone(log)
        self.assertNotIn(str(root).encode("utf-8"), log.content)
        self.assertNotIn(str(nested).encode("utf-8")[:12], log.content[-64:])
        self.assertIn(b"[runtime-scope-1]", log.content)


class _RetainedRuntimeFixture:
    def __init__(
        self,
        *,
        evaluation_delay_seconds: float = 0.0,
        evaluator_source: str | None = None,
        evaluator_settings_yaml: str | None = None,
        environment_interface: str | None = None,
        trial_workspace_seed: bool = False,
        include_second_candidate: bool = False,
        attempt_ttl_seconds: float = 300,
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "package"
        self.package_root.mkdir()
        self.include_second_candidate = include_second_candidate
        self.study_path = _write_package(self.package_root)
        if trial_workspace_seed:
            environment_path = (
                self.package_root
                / "configs"
                / "environments"
                / "environment.yaml"
            )
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8")
                + """

trialWorkspace:
  - from: seeds/input.json
    to: seeded/input.json
  - from: seeds/empty
    to: seeded/empty
  - from: seeds/tool.sh
    to: seeded/tool.sh
""",
                encoding="utf-8",
            )
            seeds = environment_path.parent / "seeds"
            seeds.mkdir()
            (seeds / "input.json").write_text(
                '{"value": 4.0}\n', encoding="utf-8"
            )
            (seeds / "empty").mkdir()
            tool = seeds / "tool.sh"
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o755)
        if environment_interface is not None:
            environment_path = (
                self.package_root
                / "configs"
                / "environments"
                / "environment.yaml"
            )
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8")
                + "\n"
                + environment_interface.strip()
                + "\n",
                encoding="utf-8",
            )
        if evaluator_settings_yaml is not None:
            environment_path = (
                self.package_root
                / "configs"
                / "environments"
                / "environment.yaml"
            )
            source = environment_path.read_text(encoding="utf-8")
            environment_path.write_text(
                source.replace(
                    "  settings: {}",
                    "  settings:\n" + evaluator_settings_yaml.rstrip(),
                    1,
                ),
                encoding="utf-8",
            )
        default_evaluator_source = (
            "from pathlib import Path\n"
            "import time\n"
            "def evaluate(candidate, context):\n"
            f"    time.sleep({float(evaluation_delay_seconds)!r})\n"
            "    path = Path(context['workspace']) / 'evaluation-count.txt'\n"
            "    path.write_text((path.read_text() if path.exists() else '') + 'x')\n"
            "    return {'score': candidate['x'], "
            "'event_summary': {'evaluation_count': len(path.read_text())}}\n"
        )
        (self.package_root / "local_package" / "evaluate.py").write_text(
            default_evaluator_source if evaluator_source is None else evaluator_source,
            encoding="utf-8",
        )
        self.ledger = RealmLedger(self.root / "realm.sqlite3")
        self.ledger.register_principal(
            operation_id="local-attempt/principal/operator",
            principal_id="operator",
            kind="human",
        )
        self.ledger.register_principal(
            operation_id="local-attempt/principal/delegate",
            principal_id="delegate",
            kind="agent",
        )
        self.store = LocalContentStore(self.root / "store", store_id="local-a")
        self.ledger.register_store(
            operation_id="local-attempt/store",
            store_id=self.store.store_id,
            backend_kind=self.store.BACKEND_KIND,
            root_marker=self.store.root_marker,
        )
        self.content = RealmContentService(
            self.ledger, local_stores={self.store.store_id: self.store}
        )
        self.projection_service = RealmProjectionService(
            self.ledger,
            local_stores={self.store.store_id: self.store},
            projection_root=self.root / "projections",
        )
        self.volume_service = RealmEphemeralVolumeService(
            self.ledger, volume_root=self.root / "volumes"
        )
        self.provider = ProcessProviderIdentity(
            builder_fingerprint="a" * 64,
            platform="test-platform",
        )
        retained = RetainedStudyService(
            self.ledger, self.content, self.projection_service, self.provider
        )
        package = retained.prepare_local_package(
            operation_id="local-attempt/package/prepare",
            actor_principal_id="operator",
            store_id=self.store.store_id,
            package_root=self.package_root,
            study_config_path=self.study_path,
            source_owner_id="local-attempt-source-owner",
            study_definition_owner_id="local-attempt-definition-owner",
        )
        self.created = retained.launch_definition_run(
            operation_id="local-attempt/run/launch",
            actor_principal_id="operator",
            controller_holder_id="local-attempt-controller",
            controller_ttl_seconds=300,
            preparation=package,
            run_id="local-attempt-run",
            owner_id="local-attempt-run-owner",
        )
        self._admit()
        self.preparation = self.ledger.prepare_run_attempt(
            operation_id="local-attempt/attempt/prepare",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            logical_trial_id="trial-a",
            attempt_id="attempt-a",
            expected_run_revision=1,
            attempt_ttl_seconds=attempt_ttl_seconds,
            **self.controller_arguments(),
        )

    def controller_arguments(self) -> dict[str, Any]:
        lease = self.created.controller_lease
        return {
            "controller_lease_id": lease.lease_id,
            "controller_holder_id": lease.holder_id,
            "controller_fencing_token": lease.fencing_token,
        }

    def _admit(self) -> None:
        candidate = NormalizedCandidateEnvelope.build(
            candidate_format="parameters", spec={"x": 0.5}
        )
        candidates = [
            CandidateAdmission(
                "candidate-a",
                candidate,
                lineage={"parents": []},
                generator={"method_id": "external"},
            )
        ]
        logical_trials = [
            LogicalTrialAdmission(
                "trial-a", "candidate-a", seed=None, repetition_index=0
            )
        ]
        if self.include_second_candidate:
            candidates.append(
                CandidateAdmission(
                    "candidate-b",
                    NormalizedCandidateEnvelope.build(
                        candidate_format="parameters", spec={"x": 0.75}
                    ),
                    lineage={"parents": []},
                    generator={"method_id": "external"},
                )
            )
            logical_trials.append(
                LogicalTrialAdmission(
                    "trial-b", "candidate-b", seed=None, repetition_index=0
                )
            )
        plan = RunAdmissionPlan(
            candidates=tuple(candidates),
            logical_trials=tuple(logical_trials),
        )
        change = self.ledger.begin_owner_change(
            operation_id="local-attempt/admission/begin",
            actor_principal_id="operator",
            owner_id=self.created.run.owner_id,
            expected_owner_revision=0,
            ttl_seconds=300,
        )
        self.ledger.commit_run_candidate_admissions(
            operation_id="local-attempt/admission/commit",
            actor_principal_id="operator",
            run_id=self.created.run.run_id,
            expected_run_revision=0,
            expected_owner_revision=0,
            change_id=change.change_id,
            plan=plan,
            **self.controller_arguments(),
        )

    def binder_for(
        self, launcher: RealmLocalAttemptLauncher
    ) -> RealmProcessExecutionBinder:
        return RealmProcessExecutionBinder(
            self.ledger,
            self.projection_service,
            self.volume_service,
            self.provider,
            launch_reservation_verifier=launcher.verify_launch_reservation,
            terminal_proof_verifier=(
                launcher._validate_supervisor_terminal_proof
            ),
        )

    def bind(self):
        """Retain the historical fixture convenience without a legacy binder API."""

        supervisor = LocalProcessSupervisor(
            self.root / "fixture-binding-provider"
        )
        launcher = RealmLocalAttemptLauncher(supervisor)
        binder = self.binder_for(launcher)
        prepared = binder.prepare_binding(
            actor_principal_id="operator", preparation=self.preparation
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        binding = prepared._commit_reserved_launch(reservation)
        # This test-only fixture exposes a managed trial scope without running
        # an evaluator.  Retain authenticated never-started evidence so the
        # finalizer's separate atomic-adoption test satisfies the production
        # rule that every bound attempt has terminal provider evidence.
        proof = launcher._abandon_reserved(reservation)
        binding.authenticate_and_record_terminal(proof)
        # Keep the provider wiring reachable for the lifetime of the fixture;
        # finalizer tests need only the attached managed resources.
        self._fixture_binding_components = (
            supervisor,
            launcher,
            binder,
            reservation,
        )
        return binding

    def close(self) -> None:
        self.store.close()
        self.ledger.close()
        self.temporary.cleanup()


@unittest.skipUnless(os.name == "posix", "local attempt process is POSIX-only")
class RealmLocalAttemptLauncherTest(unittest.TestCase):
    def test_scope_roots_accept_the_exact_optional_prepared_python_scope(self) -> None:
        spec = _compile_portable_runtime(_definition_with_prepared_python())
        root = Path("/private/optpilot-test-runtime")
        scope_paths = {item.name: root / item.name for item in spec.scopes}
        import_paths = tuple(
            scope_paths[item.scope]
            if item.relative_path == "."
            else scope_paths[item.scope].joinpath(*item.relative_path.split("/"))
            for item in spec.python_import_roots
        )
        binding = mock.Mock(
            portable_spec=spec,
            scope_paths=scope_paths,
            python_import_paths=import_paths,
            workdir=scope_paths[TRIAL_SCOPE],
        )
        launcher = object.__new__(RealmLocalAttemptLauncher)

        control, trial, source, prepared, resolved_imports = launcher._scope_roots(
            binding
        )

        self.assertEqual(control, scope_paths[CONTROL_SCOPE])
        self.assertEqual(trial, scope_paths[TRIAL_SCOPE])
        self.assertEqual(source, scope_paths[ENVIRONMENT_SOURCE_SCOPE])
        self.assertEqual(
            prepared,
            scope_paths[ENVIRONMENT_PREPARED_PYTHON_SCOPE],
        )
        self.assertEqual(resolved_imports[-1], scope_paths[ENVIRONMENT_PREPARED_PYTHON_SCOPE])

    def setUp(self) -> None:
        self.fixture = _RetainedRuntimeFixture()
        self.addCleanup(self.fixture.close)

    def _components(
        self,
        *,
        fixture: _RetainedRuntimeFixture | None = None,
        **supervisor_options: Any,
    ) -> tuple[
        LocalProcessSupervisor,
        RealmLocalAttemptLauncher,
        RealmProcessExecutionBinder,
    ]:
        target = self.fixture if fixture is None else fixture
        supervisor = LocalProcessSupervisor(
            target.root / "process-provider", **supervisor_options
        )
        launcher = RealmLocalAttemptLauncher(supervisor)
        return supervisor, launcher, target.binder_for(launcher)

    def _launch(
        self,
        *,
        fixture: _RetainedRuntimeFixture | None = None,
        **supervisor_options: Any,
    ):
        target = self.fixture if fixture is None else fixture
        supervisor, launcher, binder = self._components(
            fixture=target,
            **supervisor_options,
        )
        prepared = binder.prepare_binding(
            actor_principal_id="operator", preparation=target.preparation
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        binding = prepared._commit_reserved_launch(reservation)
        launcher._publish(compiled)
        process = launcher._start_reserved(reservation)

        def reconcile_test_launch():
            return supervisor.reconcile_terminal_launch(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
                grace_period=0.25,
                timeout=10.0,
            )

        # This helper deliberately crosses the private reserve/commit/start
        # seams instead of using LocalProcessAttemptProvider.  The provider
        # normally owns exact startup-drain recovery, so the test harness must
        # supply the equivalent final safety net before attempting an attach.
        # Register it after physical start and before any later validation can
        # raise; otherwise a failed attach followed by TemporaryDirectory
        # cleanup can orphan a live worker whose provider registry was deleted.
        self.addCleanup(reconcile_test_launch)
        try:
            handle = launcher._attach(
                binding=binding, compiled=compiled, process=process
            )
        except BaseException:
            reconcile_test_launch()
            raise
        return supervisor, launcher, binder, binding, handle

    def test_private_launch_helper_drains_started_worker_if_attach_fails(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        def reject_attach(launcher, *, binding, compiled, process):
            captured.update(launcher=launcher, compiled=compiled)
            raise RuntimeError("injected attach failure")

        with mock.patch.object(
            RealmLocalAttemptLauncher,
            "_attach",
            new=reject_attach,
        ), self.assertRaisesRegex(RuntimeError, "injected attach failure"):
            self._launch()

        launcher = captured["launcher"]
        compiled = captured["compiled"]
        supervisor = launcher._supervisor
        launch_token = compiled.worker_request.launch_token
        row = supervisor._required_row(launch_token)
        self.assertTrue(row.retired)
        self.assertIsNotNone(row.terminal)
        self.assertFalse(
            supervisor._launch_directory(row.coordinate).exists()
        )

    def test_real_retained_parameter_attempt_replays_once_with_minimal_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPTPILOT_TEST_SECRET": "do-not-forward"}):
            supervisor, launcher, binder, binding, first = self._launch()
            recovered = binder.recover(
                actor_principal_id="operator",
                run_id=self.fixture.created.run.run_id,
                attempt_id=self.fixture.preparation.attempt.attempt_id,
            )
            replay = launcher._compile(recovered)
            reservation = launcher._lookup_reservation(replay)
            launcher._publish(replay)
            second = launcher._attach(
                binding=recovered,
                compiled=replay,
                process=launcher._start_reserved(reservation),
            )

        observation = first.wait_started(timeout=5.0)
        self.assertIsInstance(observation, (WorkerStarted, WorkerTerminalProof))
        envelope = first.collect(timeout=10.0)
        replayed = second.collect(timeout=10.0)
        self.assertEqual(envelope, replayed)
        self.assertEqual(envelope.outcome, "success")
        self.assertEqual(dict(envelope.metric_values), {"score": 0.5})
        self.assertEqual(envelope.binding_id, binding.receipt.binding.binding_id)
        self.assertEqual(envelope.event_summary["evaluation_count"], 1)
        count = binding.scope_paths["trial"] / "evaluation-count.txt"
        self.assertEqual(count.read_text(encoding="utf-8"), "x")
        proof = first.terminal_proof
        self.assertIsNotNone(proof)
        self.assertEqual(proof, second.terminal_proof)
        self.assertEqual(
            launcher.validate_terminal_proof(binding, proof), proof
        )
        self.assertEqual(first.launch_request_digest, proof.launch_request_digest)

        request_path = binding.scope_paths[CONTROL_SCOPE] / ATTEMPT_REQUEST_FILE
        result_path = binding.scope_paths[CONTROL_SCOPE] / ATTEMPT_RESULT_FILE
        portable = request_path.read_bytes() + result_path.read_bytes() + proof.canonical_bytes
        self.assertNotIn(str(self.fixture.root).encode("utf-8"), portable)

        row = supervisor.database_path
        import sqlite3

        with sqlite3.connect(row) as connection:
            request_payload = json.loads(
                connection.execute(
                    "SELECT request_json FROM process_launches WHERE launch_token = ?",
                    (first.launch_token,),
                ).fetchone()[0]
            )
        env = request_payload["env"]
        self.assertEqual(
            set(env),
            {
                "PYTHONDONTWRITEBYTECODE",
                "PYTHONHASHSEED",
                "PYTHONIOENCODING",
                "PYTHONNOUSERSITE",
                "PYTHONPATH",
            },
        )
        self.assertNotIn("OPTPILOT_TEST_SECRET", env)
        self.assertEqual(
            env["PYTHONPATH"],
            os.pathsep.join(str(path) for path in binding.python_import_paths),
        )

        forged = replace(proof, launch_request_digest="f" * 64)
        with self.assertRaises(RealmConflict):
            launcher.validate_terminal_proof(binding, forged)

    def test_nested_evaluator_settings_reach_authored_code_as_json_values(self) -> None:
        fixture = _RetainedRuntimeFixture(
            evaluator_settings_yaml=(
                "    cases:\n"
                "      - id: case-a\n"
                "        values: [1, 2, 3]\n"
            ),
            evaluator_source=(
                "def evaluate(candidate, context):\n"
                "    cases = context['settings']['cases']\n"
                "    assert isinstance(cases, list)\n"
                "    assert isinstance(cases[0], dict)\n"
                "    assert isinstance(cases[0]['values'], list)\n"
                "    return {'score': float(len(cases[0]['values']))}\n"
            ),
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, _binder, _binding, handle = self._launch(
            fixture=fixture
        )

        envelope = handle.collect(timeout=10.0)

        self.assertEqual(envelope.outcome, "success")
        self.assertEqual(dict(envelope.metric_values), {"score": 3.0})

    def test_real_parameter_evaluator_can_use_seeded_trial_workspace(self) -> None:
        fixture = _RetainedRuntimeFixture(
            trial_workspace_seed=True,
            evaluator_source=(
                "import json\n"
                "import stat\n"
                "from pathlib import Path\n"
                "def evaluate(candidate, context):\n"
                "    workspace = Path(context['workspace'])\n"
                "    seed_path = workspace / 'seeded' / 'input.json'\n"
                "    seed = json.loads(seed_path.read_text())\n"
                "    assert (workspace / 'seeded' / 'empty').is_dir()\n"
                "    tool_mode = (workspace / 'seeded' / 'tool.sh').stat().st_mode\n"
                "    assert tool_mode & stat.S_IXUSR\n"
                "    seed_path.write_text('{\"value\": 99}\\n')\n"
                "    (workspace / 'derived.txt').write_text('created')\n"
                "    return {'score': seed['value'] + candidate['x']}\n"
            ),
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, binder, binding, handle = self._launch(
            fixture=fixture
        )

        envelope = handle.collect(timeout=10.0)

        self.assertEqual(envelope.outcome, "success")
        self.assertEqual(dict(envelope.metric_values), {"score": 4.5})
        trial_seed = binding.scope_paths["trial"] / "seeded" / "input.json"
        projected_seed = (
            binding.scope_paths[ENVIRONMENT_SOURCE_SCOPE]
            / "configs"
            / "environments"
            / "seeds"
            / "input.json"
        )
        self.assertEqual(trial_seed.read_text(encoding="utf-8"), '{"value": 99}\n')
        self.assertEqual(projected_seed.read_text(encoding="utf-8"), '{"value": 4.0}\n')
        self.assertNotEqual(trial_seed.stat().st_ino, projected_seed.stat().st_ino)
        self.assertEqual(
            (binding.scope_paths["trial"] / "derived.txt").read_text(
                encoding="utf-8"
            ),
            "created",
        )
        recovered = binder.recover(
            actor_principal_id="operator",
            run_id=fixture.created.run.run_id,
            attempt_id=fixture.preparation.attempt.attempt_id,
        )
        self.assertEqual(
            (recovered.scope_paths["trial"] / "seeded" / "input.json").read_text(
                encoding="utf-8"
            ),
            '{"value": 99}\n',
        )

    def test_committed_layered_recovery_requires_exact_private_proof(self) -> None:
        for case in ("missing", "boolean-fence-tamper"):
            with self.subTest(case=case):
                fixture = _RetainedRuntimeFixture(trial_workspace_seed=True)
                self.addCleanup(fixture.close)
                _supervisor, _launcher, binder, binding, handle = self._launch(
                    fixture=fixture
                )
                handle.collect(timeout=10.0)
                proof_path = (
                    binding.scope_paths["trial"].parent
                    / ".optpilot-provider-initialization.json"
                )
                if case == "missing":
                    proof_path.unlink()
                else:
                    payload = json.loads(proof_path.read_text(encoding="utf-8"))
                    payload["volume"]["usage_fencing_token"] = True
                    proof_path.chmod(0o600)
                    proof_path.write_bytes(canonical_json_bytes(payload))
                    proof_path.chmod(0o400)

                with self.assertRaisesRegex(
                    RealmIntegrityError,
                    "initialization proof",
                ):
                    binder.recover(
                        actor_principal_id="operator",
                        run_id=fixture.created.run.run_id,
                        attempt_id=fixture.preparation.attempt.attempt_id,
                    )

    def test_slow_trial_seed_copy_heartbeats_tiny_child_leases(self) -> None:
        ttl_seconds = 1.0
        fixture = _RetainedRuntimeFixture(
            trial_workspace_seed=True,
            attempt_ttl_seconds=ttl_seconds,
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, binder = self._components(fixture=fixture)
        authority = fixture.ledger.read_run_attempt_heartbeat_authority(
            actor_principal_id="operator",
            run_id=fixture.created.run.run_id,
            attempt_id=fixture.preparation.attempt.attempt_id,
        )
        heartbeat = RunAttemptHeartbeatCoordinator(
            fixture.ledger,
            actor_principal_id="operator",
            receipt=authority,
            interval_seconds=0.1,
        )
        heartbeat.start()
        self.addCleanup(heartbeat.stop)

        from optpilot.realm.layered_volume_realization import (
            realize_local_layered_volume_plan as realize,
        )

        def slow_realization(source_root, destination_fd, plan, *, progress=None):
            def delayed_progress() -> None:
                time.sleep(0.2)
                if progress is not None:
                    progress()

            return realize(
                source_root,
                destination_fd,
                plan,
                progress=delayed_progress,
            )

        started = time.monotonic()
        with mock.patch(
            "optpilot.realm.ephemeral_volume_service."
            "realize_local_layered_volume_plan",
            side_effect=slow_realization,
        ):
            prepared = binder.prepare_binding(
                actor_principal_id="operator",
                preparation=fixture.preparation,
            )
        elapsed = time.monotonic() - started

        self.assertGreater(elapsed, ttl_seconds)
        prepared.validate()
        heartbeat.raise_if_failed()
        self.assertEqual(
            (prepared.scope_paths["trial"] / "seeded" / "input.json").read_text(
                encoding="utf-8"
            ),
            '{"value": 4.0}\n',
        )

    def test_layered_seed_requires_attempt_input_role_on_environment_store(self) -> None:
        fixture = _RetainedRuntimeFixture(trial_workspace_seed=True)
        self.addCleanup(fixture.close)
        _supervisor, _launcher, binder = self._components(fixture=fixture)
        memberships = fixture.ledger.list_owner_memberships(
            actor_principal_id="operator",
            owner_id=fixture.created.run.owner_id,
        )
        attempt_inputs = tuple(
            item for item in memberships if item.role == RUN_ATTEMPT_INPUT_ROLE
        )
        self.assertEqual(len(attempt_inputs), 1)

        without_input = tuple(
            item for item in memberships if item.role != RUN_ATTEMPT_INPUT_ROLE
        )
        with mock.patch.object(
            fixture.ledger,
            "list_owner_memberships",
            return_value=without_input,
        ), self.assertRaisesRegex(RealmConflict, "no authorized store placement"):
            binder.prepare_binding(
                actor_principal_id="operator",
                preparation=fixture.preparation,
            )

        wrong_store = tuple(
            replace(item, store_id="different-local-store")
            if item.role == RUN_ATTEMPT_INPUT_ROLE
            else item
            for item in memberships
        )
        with mock.patch.object(
            fixture.ledger,
            "list_owner_memberships",
            return_value=wrong_store,
        ), self.assertRaisesRegex(RealmConflict, "no common locally available"):
            binder.prepare_binding(
                actor_principal_id="operator",
                preparation=fixture.preparation,
            )

    def test_authored_evaluator_exception_is_a_portable_semantic_result(self) -> None:
        fixture = _RetainedRuntimeFixture(
            evaluator_source=(
                "def evaluate(candidate, context):\n"
                "    raise RuntimeError('expected evaluator failure')\n"
            )
        )
        self.addCleanup(fixture.close)
        _supervisor, _launcher, _binder, binding, handle = self._launch(
            fixture=fixture
        )

        envelope = handle.collect(timeout=10.0)

        self.assertEqual(envelope.outcome, "failed")
        self.assertEqual(envelope.error["message"], "expected evaluator failure")
        self.assertIn("[runtime-scope-", envelope.error["traceback"])
        result_path = binding.scope_paths[CONTROL_SCOPE] / ATTEMPT_RESULT_FILE
        self.assertNotIn(
            str(fixture.root).encode("utf-8"), result_path.read_bytes()
        )

    def test_noncanonical_lifecycle_reserves_before_start_and_replays_once(self) -> None:
        supervisor, launcher, binder = self._components()
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        evidence_fingerprint = request_digest(
            {
                "format": "test.operator-job-evidence.v1",
                "evaluation_spec_digest": prepared.attempt.evaluation_spec.digest,
                "portable_spec_digest": prepared.portable_spec.digest,
            }
        )
        binding = LocalAttemptExecutionBinding(
            attempt_id="debug-attempt-a",
            binding_id="debug-binding-a",
            launch_token="debug-launch-a",
            evidence_fingerprint=evidence_fingerprint,
            evaluation_spec=prepared.attempt.evaluation_spec,
            portable_spec=prepared.portable_spec,
            scope_paths=prepared.scope_paths,
            validate_resources=prepared.validate,
        )

        claim = launcher.claim_noncanonical_realization(
            launch_token=binding.launch_token,
            binding_id=binding.binding_id,
        )
        try:
            reservation = launcher.reserve_noncanonical(
                binding,
                realization_claim=claim,
            )
        finally:
            launcher.release_noncanonical_realization(claim)
        self.assertEqual(supervisor.reservation_state(reservation), "reserved")
        launch_request_digest = launcher.expected_launch_request_digest(binding)
        first = launcher.start_noncanonical(
            binding=binding,
            reservation=reservation,
            launch_request_digest=launch_request_digest,
        )
        replayed_reservation, second = launcher.recover_noncanonical(
            binding=binding,
            launch_request_digest=launch_request_digest,
        )

        self.assertEqual(reservation, replayed_reservation)
        self.assertEqual(first.collect(timeout=10.0), second.collect(timeout=10.0))
        proof = first.terminal_proof
        self.assertIsNotNone(proof)
        self.assertEqual(
            launcher.retire_noncanonical(
                binding=binding,
                terminal_proof=proof,
                launch_request_digest=launch_request_digest,
            ),
            proof,
        )

    def test_tampered_result_becomes_path_free_platform_error(self) -> None:
        _supervisor, _launcher, _binder, binding, handle = self._launch()
        proof = handle.wait(timeout=10.0)
        self.assertEqual(proof.disposition, "exited")
        result_path = binding.scope_paths[CONTROL_SCOPE] / ATTEMPT_RESULT_FILE
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["envelope"]["metric_values"]["score"] = 99
        result_path.write_bytes(canonical_json_bytes(payload))

        with self.assertRaises(LocalAttemptPlatformError) as raised:
            handle.collect(timeout=1.0)
        self.assertEqual(raised.exception.code, "worker_result_invalid")
        encoded = str(raised.exception).encode("utf-8")
        self.assertNotIn(str(self.fixture.root).encode("utf-8"), encoded)
        self.assertFalse(hasattr(raised.exception, "to_dict"))

    def test_intent_before_spawn_replays_as_never_started(self) -> None:
        def crash(point: str) -> None:
            self.assertEqual(point, "intent_committed")
            raise _SimulatedParentCrash()

        _supervisor, launcher, binder = self._components(fault_injector=crash)
        prepared = binder.prepare_binding(
            actor_principal_id="operator",
            preparation=self.fixture.preparation,
        )
        compiled = launcher._compile(prepared)
        reservation = launcher._reserve(compiled)
        binding = prepared._commit_reserved_launch(reservation)
        launcher._publish(compiled)
        with self.assertRaises(_SimulatedParentCrash):
            launcher._start_reserved(reservation)

        _restarted_supervisor, restarted, restarted_binder = self._components()
        recovered = restarted_binder.recover(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            attempt_id=self.fixture.preparation.attempt.attempt_id,
        )
        replay = restarted._compile(recovered)
        replayed_reservation = restarted._lookup_reservation(replay)
        restarted._publish(replay)
        handle = restarted._attach(
            binding=recovered,
            compiled=replay,
            process=restarted._start_reserved(replayed_reservation),
        )
        observation = handle.wait_started(timeout=3.0)
        self.assertIsInstance(observation, WorkerTerminalProof)
        self.assertEqual(observation.disposition, "never_started")
        with self.assertRaises(LocalAttemptPlatformError) as raised:
            handle.collect(timeout=1.0)
        self.assertEqual(raised.exception.code, "worker_never_started")
        count = binding.scope_paths["trial"] / "evaluation-count.txt"
        self.assertFalse(count.exists())


if __name__ == "__main__":
    unittest.main()


class ContainerEnvironmentAttemptTest(unittest.TestCase):
    """Each candidate evaluates inside a fresh container.

    Real container software, real retained package, real supervision: the same
    wrapper that runs a host evaluator runs the engine client here, holding the
    same liveness lock and publishing the same terminal proof. The freshness
    canary is the point of the whole design: the evaluator RAISES if it can see
    anything a previous candidate wrote to the container's filesystem, so two
    green attempts prove nothing carries over.
    """

    def setUp(self) -> None:
        from optpilot.container_engine import (
            ContainerEngineError,
            inspect_image,
            resolve_container_engine,
        )

        try:
            self.engine = resolve_container_engine()
            inspection = inspect_image(self.engine, "python:3.12-slim")
        except ContainerEngineError as error:
            self.skipTest(f"container engine unavailable: {error}")
        if inspection is None:
            self.skipTest("python:3.12-slim is not present locally")
        runtime_yaml = (
            "runtime:\n"
            "  sandbox: container\n"
            "  container:\n"
            f"    image: {inspection.config_digest}\n"
            f"    platform: {inspection.platform}\n"
        )
        canary = (
            "from pathlib import Path\n"
            "def evaluate(candidate, context):\n"
            "    marker = Path('/tmp/optpilot-fresh-canary')\n"
            "    if marker.exists():\n"
            "        raise RuntimeError('container was reused between candidates')\n"
            "    marker.write_text('seen')\n"
            "    return {'score': candidate['x']}\n"
        )
        self.fixture = _RetainedRuntimeFixture(
            evaluator_source=canary,
            environment_interface=runtime_yaml,
            include_second_candidate=True,
        )
        self.addCleanup(self.fixture.temporary.cleanup)
        self.supervisor = LocalProcessSupervisor(
            self.fixture.root / "process-provider"
        )
        self.launcher = RealmLocalAttemptLauncher(self.supervisor)
        self.binder = RealmProcessExecutionBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
            self.fixture.provider,
            trust_policy=SimpleNamespace(
                read_active=lambda **_kw: SimpleNamespace(state="approved")
            ),
            launch_reservation_verifier=self.launcher.verify_launch_reservation,
            terminal_proof_verifier=(
                self.launcher._validate_supervisor_terminal_proof
            ),
        )

    def _evaluate(self, preparation):
        prepared = self.binder.prepare_binding(
            actor_principal_id="operator", preparation=preparation
        )
        self.assertIsNotNone(prepared.container_plan)
        compiled = self.launcher._compile(prepared)
        argv = compiled.process_request.argv
        self.assertEqual(argv[0], self.engine)
        self.assertIn("--pull", argv)
        self.assertIn("--network", argv)
        reservation = self.launcher._reserve(compiled)
        binding = prepared._commit_reserved_launch(reservation)
        self.launcher._publish(compiled)
        process = self.launcher._start_reserved(reservation)

        def reconcile():
            return self.supervisor.reconcile_terminal_launch(
                launch_token=reservation.launch_token,
                binding_id=reservation.binding_id,
                evidence_fingerprint=reservation.evidence_fingerprint,
                launch_request_digest=reservation.launch_request_digest,
                grace_period=0.25,
                timeout=30.0,
            )

        self.addCleanup(reconcile)
        handle = self.launcher._attach(
            binding=binding, compiled=compiled, process=process
        )
        return binding, handle.collect(timeout=120.0)

    def test_two_candidates_get_two_fresh_containers(self) -> None:
        binding, first = self._evaluate(self.fixture.preparation)
        self.assertEqual(first.outcome, "success", first)
        self.assertEqual(dict(first.metric_values), {"score": 0.5})

        current = self.fixture.ledger.read_run_snapshot(
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
        )
        second_preparation = self.fixture.ledger.prepare_run_attempt(
            operation_id="local-attempt/attempt/prepare-b",
            actor_principal_id="operator",
            run_id=self.fixture.created.run.run_id,
            logical_trial_id="trial-b",
            attempt_id="attempt-b",
            expected_run_revision=current.revision.revision,
            attempt_ttl_seconds=300,
            **self.fixture.controller_arguments(),
        )
        _, second = self._evaluate(second_preparation)
        # The canary raises on any residue; success here IS the freshness proof.
        self.assertEqual(second.outcome, "success", second)

        import subprocess as _sp

        for token in (
            self.fixture.preparation.attempt.launch_token,
            second_preparation.attempt.launch_token,
        ):
            name = RealmLocalAttemptLauncher.container_attempt_name(token)
            probe = _sp.run(
                [self.engine, "container", "inspect", name],
                capture_output=True,
                timeout=60,
            )
            self.assertNotEqual(probe.returncode, 0, f"{name} is gone after use")

    def test_without_an_approval_nothing_starts(self) -> None:
        binder = RealmProcessExecutionBinder(
            self.fixture.ledger,
            self.fixture.projection_service,
            self.fixture.volume_service,
            self.fixture.provider,
            launch_reservation_verifier=self.launcher.verify_launch_reservation,
        )
        with self.assertRaises(RealmConflict) as caught:
            binder.prepare_binding(
                actor_principal_id="operator",
                preparation=self.fixture.preparation,
            )
        self.assertIn("approved for study execution", str(caught.exception))
