"""Studio surface for headless resource actions (U1b over F4)."""

from __future__ import annotations

import tempfile
import time
import unittest
from http import HTTPStatus
from pathlib import Path

import yaml

from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _resource_action_run_status,
    _start_resource_action_run,
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


class StudioResourceActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "demo_package"
        resource_dir = self.package / "resources" / "demo-generator"
        resource_dir.mkdir(parents=True)
        (resource_dir / "generate.py").write_text(
            """
import json, os, pathlib, sys
inputs = json.loads(
    pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"]).read_text()
)
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "bundle.json").write_text(json.dumps({"inputs": inputs, "argv": sys.argv[1:]}))
print("bundle generated")
""",
            encoding="utf-8",
        )
        (resource_dir / "optpilot.resource.yaml").write_text(
            yaml.safe_dump(
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
                            "command": [
                                "python",
                                "generate.py",
                                "--name",
                                "{input:name}",
                            ],
                            "inputs": {
                                "name": {"valueType": "string"},
                                "count": {"valueType": "int", "default": 2},
                            },
                            "timeoutSeconds": 60,
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.state = UiState(
            cwd=self.root, catalog_roots=[self.package], run_roots=[]
        )
        self.addCleanup(self.state.close_coordination)

    def _await_run(self, request_id: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = _resource_action_run_status(self.state, request_id)
            if status["status"] != "running":
                return status
            if time.monotonic() >= deadline:
                self.fail("Resource action run did not settle.")
            time.sleep(0.05)

    def test_catalog_exposes_declared_actions_in_raw_config(self) -> None:
        resources = _catalog_payload(self.state)["resources"]
        entry = next(item for item in resources if item["id"] == "demo-generator")
        actions = entry["raw_config"]["actions"]
        self.assertEqual(actions[0]["id"], "generate")
        self.assertIn("name", actions[0]["inputs"])

    def test_run_executes_typed_inputs_and_replays_idempotently(self) -> None:
        resources = _catalog_payload(self.state)["resources"]
        entry = next(item for item in resources if item["id"] == "demo-generator")
        request = {
            "request_id": "82345678-1234-4234-8234-123456789abc",
            "resource_uid": entry["uid"],
            "action_id": "generate",
            "inputs": {"name": "Ada"},
        }

        accepted, status = _start_resource_action_run(self.state, request)
        self.assertEqual(status, HTTPStatus.ACCEPTED)
        self.assertEqual(accepted["status"], "running")

        settled = self._await_run(request["request_id"])
        self.assertEqual(settled["status"], "succeeded", settled)
        result = settled["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["path"] for item in result["outputs"]], ["bundle.json"]
        )
        self.assertIn("bundle generated", result["stdout_tail"])

        replayed, replay_status = _start_resource_action_run(self.state, request)
        self.assertEqual(replay_status, HTTPStatus.OK)
        self.assertEqual(replayed["status"], "succeeded")

    def test_invalid_inputs_fail_the_run_with_the_core_error(self) -> None:
        resources = _catalog_payload(self.state)["resources"]
        entry = next(item for item in resources if item["id"] == "demo-generator")
        request = {
            "request_id": "92345678-1234-4234-8234-123456789abc",
            "resource_uid": entry["uid"],
            "action_id": "generate",
            "inputs": {},
        }
        _accepted, _status = _start_resource_action_run(self.state, request)
        settled = self._await_run(request["request_id"])
        self.assertEqual(settled["status"], "failed")
        self.assertIn("required", str(settled["error"]))

    def test_unknown_action_is_rejected_before_any_run_record(self) -> None:
        resources = _catalog_payload(self.state)["resources"]
        entry = next(item for item in resources if item["id"] == "demo-generator")
        with self.assertRaisesRegex(ValueError, "declared actions"):
            _start_resource_action_run(
                self.state,
                {
                    "request_id": "a2345678-1234-4234-8234-123456789abc",
                    "resource_uid": entry["uid"],
                    "action_id": "absent",
                },
            )
        with self.assertRaises(KeyError):
            _resource_action_run_status(
                self.state, "a2345678-1234-4234-8234-123456789abc"
            )

    def test_client_renders_action_forms_and_run_controls(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("function resourceActionsPanel(", source)
        self.assertIn("data-run-resource-action", source)
        self.assertIn("/api/resource-actions/run", source)
        self.assertIn("resourceActionsPanel(item)", source)
        self.assertIn("bindResourceActionControls(item)", source)
        self.assertIn("<h3>Headless actions</h3>", source)
        self.assertIn("Declared in this Resource's YAML under <code>actions</code>", source)


if __name__ == "__main__":
    unittest.main()
