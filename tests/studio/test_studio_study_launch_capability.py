from __future__ import annotations

import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from optpilot.realm.operator_job_records import (
    OperatorJobCleanupState,
    OperatorJobState,
)
from optpilot.retained_study_compiler import RetainedStudyCompileError

from optpilot_studio.ui.server import (
    UiState,
    _catalog_detail,
    _catalog_payload,
    _create_agent_session,
    _execute_agent_tool,
    _execute_study_launch_request,
    _handler_factory,
    _schedule_study_launch_execution,
    _study_launch_preparation_status,
    _studio_method_environment_binding,
    _studio_method_environment_values,
    _submit_study_launch_request,
    _update_agent_settings,
    _validate_study,
)


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


def _function_source(source: str, declaration: str, next_declaration: str) -> str:
    start = source.index(declaration)
    end = source.index(next_declaration, start)
    return source[start:end]


class StudioStudyLaunchCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _write_package(
        self,
        *,
        protocol: str,
        method_env: tuple[str, ...] = (),
        study_inputs: dict | None = None,
    ) -> tuple[Path, Path, Path]:
        package = self.root / f"package-{protocol}"
        environment_dir = package / "environments" / "toy"
        method_dir = package / "methods" / protocol
        studies_dir = package / "studies"
        environment_dir.mkdir(parents=True)
        method_dir.mkdir(parents=True)
        studies_dir.mkdir(parents=True)
        marker = self.root / f"imported-{protocol}.txt"
        (environment_dir / "evaluator.py").write_text(
            "def evaluate(candidate_runtime, context):\n"
            "    return {'score': 1.0}\n",
            encoding="utf-8",
        )
        (environment_dir / "environment.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "optpilot.io/v1",
                    "config": "environment",
                    "id": "toy",
                    "evaluator": {
                        "python": "evaluator:evaluate",
                        "pythonPath": ["."],
                    },
                    "candidate": {
                        "format": "parameters",
                        "parameters": {
                            "schema": {
                                "x": {
                                    "valueType": "float",
                                    "min": 0,
                                    "max": 1,
                                }
                            }
                        },
                    },
                    "metrics": {"source": "return", "keys": ["score"]},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (method_dir / "method.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported')\n"
            "class Method:\n"
            "    def __init__(self, definition, study_spec, rng): pass\n"
            "    def propose(self, n_candidates, study_state): return []\n",
            encoding="utf-8",
        )
        method_config = {
            "apiVersion": "optpilot.io/v1",
            "config": "method",
            "id": protocol,
            "entrypoint": {
                "python": "method:Method",
                "pythonPath": ["."],
                "protocol": protocol,
            },
            "accepts": {"formats": ["parameters"]},
        }
        if method_env:
            method_config["runtime"] = {
                "sandbox": "process",
                "envFromHost": list(method_env),
            }
        (method_dir / "method.yaml").write_text(
            yaml.safe_dump(
                method_config,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        study = studies_dir / "study.yaml"
        study_config = {
            "apiVersion": "optpilot.io/v1",
            "config": "study",
            "name": f"{protocol}-study",
            "environmentConfig": "../environments/toy/environment.yaml",
            "methodConfig": f"../methods/{protocol}/method.yaml",
            "objective": {"metric": "score", "direction": "maximize"},
            "budget": {"maxTrials": 1},
        }
        if study_inputs is not None:
            study_config["inputs"] = study_inputs
        study.write_text(
            yaml.safe_dump(
                study_config,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return package, study, marker

    @staticmethod
    def _handler(state: UiState):
        handler = object.__new__(_handler_factory(state))
        responses = []
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
        )
        return handler, responses

    def test_static_validation_reports_exact_unsupported_retained_capability(self) -> None:
        _package, study, marker = self._write_package(protocol="session")

        validation = _validate_study(study)

        self.assertTrue(validation["valid"], validation)
        self.assertFalse(validation["launch"]["eligible"])
        self.assertEqual(validation["launch"]["code"], "method_mode_unsupported")
        retained = validation["capabilities"]["retained_execution"]
        self.assertFalse(retained["supported"])
        self.assertEqual(retained["verification"], "static")
        self.assertFalse(marker.exists(), "Validate must not import authored Python.")

    def test_catalog_detail_and_list_expose_the_same_launch_capability(self) -> None:
        package, _study, _marker = self._write_package(protocol="session")
        state = UiState(cwd=self.root, catalog_roots=[package], run_roots=[])

        catalog = _catalog_payload(state)
        listed = catalog["studies"][0]
        detail = _catalog_detail(state, "study", listed["uid"])

        self.assertTrue(str(listed["path"]).startswith("catalog://"))
        self.assertIsInstance(listed.get("ref"), dict)
        self.assertEqual(detail["entry"].get("ref"), listed.get("ref"))
        self.assertEqual(
            listed["validation"]["launch"],
            detail["validation"]["launch"],
        )
        self.assertEqual(
            detail["validation"]["launch"]["code"],
            "method_mode_unsupported",
        )

    def test_http_and_state_launch_gates_block_before_process_creation(self) -> None:
        package, study, _marker = self._write_package(protocol="session")
        state = UiState(cwd=self.root, catalog_roots=[package], run_roots=[])
        self.addCleanup(state.close_coordination)
        handler, responses = self._handler(state)

        handler.path = "/api/studies/validate"
        handler._read_json_body = lambda: {"study_path": str(study)}  # type: ignore[method-assign]
        handler.do_POST()
        validated, validate_status = responses[-1]
        self.assertEqual(validate_status, HTTPStatus.OK)
        self.assertFalse(validated["launch"]["eligible"])

        with mock.patch.object(state, "launch_study") as launch:
            handler.path = "/api/studies/launch"
            handler._read_json_body = lambda: {  # type: ignore[method-assign]
                "schema": "optpilot.studio-study-launch-request.v1",
                "request_id": "12345678-1234-4234-8234-123456789abc",
                "study_path": str(study),
            }
            handler.do_POST()
        accepted, accepted_status = responses[-1]
        self.assertEqual(accepted_status, HTTPStatus.ACCEPTED)
        self.assertEqual(accepted["state"], "preparing")
        deadline = time.monotonic() + 5
        while True:
            rejected = _study_launch_preparation_status(
                state, "12345678-1234-4234-8234-123456789abc"
            )
            if rejected["state"] == "failed":
                break
            if time.monotonic() >= deadline:
                self.fail("Unsupported Study preparation did not settle.")
            time.sleep(0.01)
        self.assertEqual(rejected["failure"]["code"], "method_mode_unsupported")
        self.assertIn("requires a Python", rejected["failure"]["message"])
        launch.assert_not_called()

        with mock.patch("optpilot_studio.ui.server.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "method_mode_unsupported"):
                state.launch_study(study)
        popen.assert_not_called()

    def test_declared_launch_inputs_are_static_valid_and_exposed(self) -> None:
        declaration = {
            "problem": {"valueType": "string", "description": "Problem text"},
            "iterations": {"valueType": "int", "min": 1, "default": 3},
        }
        _package, study, marker = self._write_package(
            protocol="batch", study_inputs=declaration
        )

        validation = _validate_study(study)

        # A required (no-default) input must not fail static validation; the
        # launch form supplies its value at launch time.
        self.assertTrue(validation["valid"], validation)
        self.assertTrue(validation["launch"]["eligible"], validation["launch"])
        self.assertEqual(validation["inputs"], declaration)
        self.assertFalse(marker.exists(), "Validate must not import authored Python.")

    def test_study_without_inputs_reports_none(self) -> None:
        _package, study, _marker = self._write_package(protocol="batch")
        validation = _validate_study(study)
        self.assertTrue(validation["valid"], validation)
        self.assertIsNone(validation["inputs"])

    def test_launch_request_carries_typed_inputs_to_the_core_launch(self) -> None:
        declaration = {"problem": {"valueType": "string"}}
        _package, study, _marker = self._write_package(
            protocol="batch", study_inputs=declaration
        )
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)
        launch_error = ValueError("stop before realm work")
        with mock.patch.object(
            state, "launch_study", side_effect=launch_error
        ) as launch:
            response, status = _execute_study_launch_request(
                state,
                {
                    "schema": "optpilot.studio-study-launch-request.v1",
                    "request_id": "42345678-1234-4234-8234-123456789abc",
                    "study_path": str(study),
                    "inputs": {"problem": "maximize widget output"},
                },
            )

        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        launch.assert_called_once()
        self.assertEqual(
            launch.call_args.kwargs["launch_inputs"],
            {"problem": "maximize widget output"},
        )

    def test_launch_request_rejects_malformed_inputs(self) -> None:
        _package, study, _marker = self._write_package(protocol="batch")
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)
        with mock.patch.object(state, "launch_study") as launch:
            with self.assertRaisesRegex(ValueError, "JSON object"):
                _execute_study_launch_request(
                    state,
                    {
                        "schema": "optpilot.studio-study-launch-request.v1",
                        "request_id": "52345678-1234-4234-8234-123456789abc",
                        "study_path": str(study),
                        "inputs": ["not", "a", "mapping"],
                    },
                )
        launch.assert_not_called()

    def test_client_renders_and_submits_launch_input_values(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        render = _function_source(
            source,
            "function renderPlanDetail()",
            "function studyConfigEditor(",
        )
        launch = _function_source(
            source,
            "async function launchPlan(",
            "function persistActiveStudyLaunch(",
        )
        collect = _function_source(
            source,
            "function collectStudyLaunchInputs(",
            "function studyReadinessRows(",
        )

        self.assertIn("studyLaunchInputsPanel(plan)", render)
        self.assertIn("bindStudyLaunchInputControls(plan)", render)
        self.assertIn("collectStudyLaunchInputs(plan)", launch)
        self.assertIn("request.inputs = launchInputs.values", launch)
        self.assertIn('"default" in item', collect)

    def test_supported_batch_study_is_launchable_without_importing_python(self) -> None:
        _package, study, marker = self._write_package(protocol="batch")

        validation = _validate_study(study)

        self.assertTrue(validation["valid"], validation)
        self.assertTrue(validation["launch"]["eligible"])
        self.assertEqual(validation["launch"]["code"], "ready")
        self.assertFalse(marker.exists(), "Preflight must remain non-executing.")

    def test_method_local_value_is_setup_not_a_reproducibility_failure(self) -> None:
        _package, study, marker = self._write_package(
            protocol="batch", method_env=("TEST_METHOD_API_KEY",)
        )
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)

        with mock.patch.dict(
            "os.environ", {"TEST_METHOD_API_KEY": ""}, clear=False
        ):
            missing = _validate_study(study, state=state)
            with self.assertRaisesRegex(
                ValueError, "runtime_environment_missing"
            ):
                state.launch_study(study)
            _update_agent_settings(
                state,
                {
                    "openhands": {"enabled": False},
                    "environment": {
                        "set": [
                            {
                                "name": "TEST_METHOD_API_KEY",
                                "value": "studio-only-value",
                            }
                        ]
                    },
                },
            )
            configured = _validate_study(study, state=state)

        self.assertTrue(missing["valid"], missing)
        self.assertTrue(
            missing["capabilities"]["retained_execution"]["eligible"], missing
        )
        self.assertEqual(
            missing["launch"]["code"], "runtime_environment_missing"
        )
        self.assertEqual(
            missing["runtime_environment"]["missing_names"],
            ["TEST_METHOD_API_KEY"],
        )
        self.assertTrue(configured["launch"]["eligible"], configured)
        self.assertEqual(configured["launch"]["code"], "ready")
        self.assertEqual(
            configured["runtime_environment"]["requirements"],
            [
                {
                    "name": "TEST_METHOD_API_KEY",
                    "component": "Method",
                    "configured": True,
                    "source": "settings",
                }
            ],
        )
        self.assertNotIn("studio-only-value", repr(configured))
        self.assertFalse(marker.exists(), "Validation must not import authored Python.")

    def test_retained_run_binds_one_settings_revision_without_copying_value(self) -> None:
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)
        _update_agent_settings(
            state,
            {
                "openhands": {"enabled": False},
                "environment": {
                    "set": [
                        {
                            "name": "TEST_METHOD_API_KEY",
                            "value": "first-private-value",
                        }
                    ]
                },
            },
        )

        first = _studio_method_environment_binding(
            state, ["TEST_METHOD_API_KEY"]
        )
        same = _studio_method_environment_binding(
            state, ["TEST_METHOD_API_KEY"]
        )
        self.assertEqual(same, first)
        self.assertNotIn("first-private-value", repr(first))
        self.assertEqual(
            _studio_method_environment_values(state, first),
            {"TEST_METHOD_API_KEY": "first-private-value"},
        )

        _update_agent_settings(
            state,
            {
                "openhands": {"enabled": False},
                "environment": {
                    "set": [
                        {
                            "name": "TEST_METHOD_API_KEY",
                            "value": "second-private-value",
                        }
                    ]
                },
            },
        )
        second = _studio_method_environment_binding(
            state, ["TEST_METHOD_API_KEY"]
        )
        self.assertNotEqual(
            second["binding_revision"], first["binding_revision"]
        )
        with self.assertRaisesRegex(ValueError, "original Studio Settings"):
            _studio_method_environment_values(state, first)
        self.assertEqual(
            _studio_method_environment_values(state, second),
            {"TEST_METHOD_API_KEY": "second-private-value"},
        )

    def test_host_only_value_is_not_treated_as_a_retained_run_binding(self) -> None:
        _package, study, _marker = self._write_package(
            protocol="batch", method_env=("TEST_METHOD_API_KEY",)
        )
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)

        with mock.patch.dict(
            "os.environ", {"TEST_METHOD_API_KEY": "host-only-value"}, clear=False
        ):
            validation = _validate_study(study, state=state)

        self.assertEqual(
            validation["launch"]["code"], "runtime_environment_missing"
        )
        self.assertEqual(
            validation["runtime_environment"]["requirements"],
            [
                {
                    "name": "TEST_METHOD_API_KEY",
                    "component": "Method",
                    "configured": False,
                    "source": "host-unbound",
                }
            ],
        )

    def test_study_dispatch_passes_only_resolved_method_values(self) -> None:
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)
        _update_agent_settings(
            state,
            {
                "openhands": {"enabled": False},
                "environment": {
                    "set": [
                        {"name": "DECLARED_METHOD_KEY", "value": "run-value"},
                        {"name": "UNDECLARED_KEY", "value": "must-not-pass"},
                    ]
                },
            },
        )
        retained_binding = _studio_method_environment_binding(
            state, ["DECLARED_METHOD_KEY"]
        )
        terminal = SimpleNamespace(
            launch_id="launch-test",
            job=SimpleNamespace(
                state=OperatorJobState.SUCCEEDED,
                cleanup_state=OperatorJobCleanupState.COMPLETE,
            ),
        )
        captured: list[dict[str, str]] = []
        called = threading.Event()
        test_case = self

        class Service:
            @staticmethod
            def read(*, launch_id: str):
                test_case.assertEqual(launch_id, "launch-test")
                return terminal

            @staticmethod
            def execute(*, launch_id: str, method_environment):
                test_case.assertEqual(launch_id, "launch-test")
                captured.append(method_environment.process_environment())
                method_environment.discard_values()
                called.set()
                return terminal

        with (
            mock.patch(
                "optpilot_studio.ui.server._study_launch_service_for_state",
                return_value=Service(),
            ),
            mock.patch(
                "optpilot_studio.ui.server._study_launch_method_environment_binding",
                return_value=retained_binding,
            ),
            mock.patch.dict(
                "os.environ",
                {"DECLARED_METHOD_KEY": "host-value"},
                clear=False,
            ),
        ):
            self.assertTrue(
                _schedule_study_launch_execution(
                    state, launch_id="launch-test"
                )
            )
            self.assertTrue(called.wait(5), "Study dispatch did not run.")

        self.assertEqual(captured, [{"DECLARED_METHOD_KEY": "run-value"}])

    def test_launch_submission_returns_while_durable_preparation_runs(self) -> None:
        state = UiState(cwd=self.root, catalog_roots=[], run_roots=[])
        self.addCleanup(state.close_coordination)
        entered = threading.Event()
        release = threading.Event()
        request_id = "12345678-1234-4234-8234-123456789abc"
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": request_id,
            "study_path": str(self.root / "study.yaml"),
        }

        def hold_preparation(_state, payload):
            self.assertEqual(payload, request)
            entered.set()
            release.wait(5)
            return ({}, HTTPStatus.CREATED)

        try:
            with mock.patch(
                "optpilot_studio.ui.server._execute_study_launch_request",
                side_effect=hold_preparation,
            ) as execute:
                started = time.monotonic()
                response, status = _submit_study_launch_request(state, request)
                elapsed = time.monotonic() - started
                self.assertEqual(status, HTTPStatus.ACCEPTED)
                self.assertLess(elapsed, 1.0)
                self.assertEqual(response["state"], "preparing")
                self.assertEqual(response["request_id"], request_id)
                self.assertTrue(entered.wait(2), "Preparation worker did not start.")

                duplicate, duplicate_status = _submit_study_launch_request(
                    state, request
                )
                self.assertEqual(duplicate_status, HTTPStatus.ACCEPTED)
                self.assertEqual(duplicate["request_id"], request_id)
                self.assertEqual(execute.call_count, 1)

                observed = _study_launch_preparation_status(state, request_id)
                self.assertEqual(observed["state"], "preparing")
                self.assertIsNone(observed["launch"])
                self.assertGreaterEqual(observed["elapsed_seconds"], 0)
        finally:
            release.set()

    def test_background_launch_preserves_a_typed_compile_failure_code(self) -> None:
        package, study, _marker = self._write_package(protocol="batch")
        state = UiState(cwd=self.root, catalog_roots=[package], run_roots=[])
        self.addCleanup(state.close_coordination)
        request = {
            "schema": "optpilot.studio-study-launch-request.v1",
            "request_id": "22345678-1234-4234-8234-123456789abc",
            "study_path": str(study),
        }

        with mock.patch.object(
            state,
            "launch_study",
            side_effect=RetainedStudyCompileError(
                "retained_contract_changed",
                "The retained Study contract changed.",
            ),
        ):
            response, status = _execute_study_launch_request(state, request)

        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response["code"], "retained_contract_changed")
        self.assertEqual(response["error"], "The retained Study contract changed.")

    def test_assistant_launch_is_blocked_before_permission_or_execution(self) -> None:
        package, _study, _marker = self._write_package(protocol="session")
        state = UiState(cwd=self.root, catalog_roots=[package], run_roots=[])
        session = _create_agent_session(state, {"title": "Unsupported launch"})
        study_ref = _catalog_payload(state)["studies"][0]["ref"]

        with mock.patch(
            "optpilot_studio.ui.server._agent_permission_gate"
        ) as permission_gate, mock.patch.object(state, "launch_study") as launch:
            result = _execute_agent_tool(
                state,
                session["id"],
                "optpilot_study_launch",
                {"study_ref": study_ref},
            )

        self.assertFalse(result["ok"], result)
        capability = result["data"]["capability"]
        self.assertEqual(capability["code"], "method_mode_unsupported")
        self.assertFalse(capability["eligible"])
        permission_gate.assert_not_called()
        launch.assert_not_called()

    def test_client_disables_and_explains_unsupported_study_launch(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        render = _function_source(
            source,
            "function renderPlanDetail()",
            "function studyConfigEditor(",
        )
        launch = _function_source(
            source,
            "async function launchPlan(",
            "function persistActiveStudyLaunch(",
        )
        validation = _function_source(
            source,
            "function validationHtml(",
            "async function getJson(",
        )

        self.assertIn("studyLaunchCapability(plan)", render)
        self.assertIn("launchCapability.eligible === true", render)
        self.assertIn("Run setup launch unavailable", launch)
        self.assertIn("knownCapability.eligible !== true", launch)
        self.assertIn("Launch unavailable", validation)
        self.assertIn("launch.code", validation)


if __name__ == "__main__":
    unittest.main()
