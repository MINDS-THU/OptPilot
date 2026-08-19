"""The Assistant can run the tools that make things, and keep what they made.

A Resource is how OptPilot exposes something that produces an artefact -- most
importantly generating a simulator from a plain-language description. That was
reachable only by a person clicking in the browser, so the Assistant could
describe the first leg of OptPilot's headline story but never take it.

The half that matters is not the tool, it is where the output goes. Action
results were written into Studio's own runtime folder, which is fine to read
and useless to build on: a generated bundle there has to be copied out by hand
before it can be registered, and the copy severs the link between what was
generated and what was registered. So an action may name an attached Workspace
and write its results inside it, ready to register in place.
"""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path

import yaml

from optpilot_studio.ui.server import (
    DEFAULT_ASSISTANT_PERMISSIONS,
    UiState,
    _approve_agent_action,
    _attach_agent_workspace,
    _catalog_payload,
    _create_agent_session,
    _create_ui_workspace,
    _execute_agent_tool,
    _read_agent_approvals,
    _resource_action_run_status,
    _update_agent_settings,
)

_GENERATOR = """
import json, os, pathlib
inputs = json.loads(
    pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"]).read_text()
)
out = pathlib.Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])
(out / "bundle.json").write_text(json.dumps({"inputs": inputs}))
print("bundle generated")
"""


class AssistantResourceActionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "demo_package"
        resource_dir = self.package / "resources" / "demo-generator"
        resource_dir.mkdir(parents=True)
        (resource_dir / "generate.py").write_text(_GENERATOR, encoding="utf-8")
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
                            "description": "Write a bundle from a description.",
                            "command": ["python", "generate.py"],
                            "inputs": {"name": {"valueType": "string"}},
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
        for name in (
            "sessions_dir",
            "agent_sessions_dir",
            "jobs_dir",
            "workspaces_dir",
            "runtime_dir",
        ):
            setattr(self.state, name, self.root / name)
            getattr(self.state, name).mkdir(parents=True, exist_ok=True)
        self.state.settings_path = self.root / "settings.json"
        _update_agent_settings(self.state, {"openhands": {"enabled": False}})
        self.session = _create_agent_session(self.state, {"title": "Actions"})
        self.resource_uid = _catalog_payload(self.state)["resources"][0]["uid"]

    def _await(self, request_id: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = _resource_action_run_status(self.state, request_id)
            if status["status"] != "running":
                return status
            if time.monotonic() >= deadline:
                self.fail("the action did not settle")
            time.sleep(0.05)

    # ---- listing --------------------------------------------------------
    def test_the_assistant_can_see_what_a_resource_offers(self) -> None:
        result = _execute_agent_tool(
            self.state,
            self.session["id"],
            "optpilot_resource_action_list",
            {"resource_uid": self.resource_uid},
        )
        self.assertTrue(result["ok"], result)
        actions = result["data"]["actions"]
        self.assertEqual([a["action_id"] for a in actions], ["generate"])
        self.assertEqual(actions[0]["label"], "Generate a bundle")
        self.assertIn("name", actions[0]["inputs"])

    # ---- approval -------------------------------------------------------
    def test_running_an_action_asks_first(self) -> None:
        self.assertEqual(
            DEFAULT_ASSISTANT_PERMISSIONS["resource_action"], "approval_required"
        )
        result = _execute_agent_tool(
            self.state,
            self.session["id"],
            "optpilot_resource_action_run",
            {
                "resource_uid": self.resource_uid,
                "action_id": "generate",
                "inputs": {"name": "demo"},
            },
        )
        self.assertFalse(result["ok"], result)
        self.assertTrue(result["data"]["approval_required"])
        self.assertEqual(len(_read_agent_approvals(self.state, self.session["id"])), 1)

    def test_a_bad_request_is_refused_before_anyone_is_asked(self) -> None:
        # Rejected the same way every other tool rejects a bad argument, and
        # -- the part that matters -- without leaving an approval card behind
        # for something that could never have run.
        with self.assertRaises(ValueError):
            _execute_agent_tool(
                self.state,
                self.session["id"],
                "optpilot_resource_action_run",
                {"resource_uid": self.resource_uid, "action_id": ""},
            )
        self.assertEqual(_read_agent_approvals(self.state, self.session["id"]), [])

    # ---- the point of the whole thing -----------------------------------
    def test_approved_output_lands_inside_the_attached_workspace(self) -> None:
        workspace = _create_ui_workspace(
            self.state,
            {"title": "Generated", "root": str(self.root / "workspace")},
        )
        _attach_agent_workspace(
            self.state, self.session["id"], workspace["id"], select=True
        )
        started = _execute_agent_tool(
            self.state,
            self.session["id"],
            "optpilot_resource_action_run",
            {
                "resource_uid": self.resource_uid,
                "action_id": "generate",
                "inputs": {"name": "demo"},
                "workspace_id": workspace["id"],
            },
        )
        self.assertFalse(started["ok"], "it must ask before running")
        approval = _read_agent_approvals(self.state, self.session["id"])[0]
        approved = _approve_agent_action(
            self.state, self.session["id"], approval["id"]
        )
        self.assertTrue(approved["result"]["ok"], approved)

        request_id = approved["result"]["data"]["request_id"]
        final = self._await(request_id)
        self.assertEqual(final["status"], "succeeded", final)

        workspace_root = Path(workspace["root"]).resolve()
        output_root = Path(final["result"]["output_root"]).resolve()
        self.assertTrue(
            str(output_root).startswith(str(workspace_root)),
            f"{output_root} is not inside {workspace_root}",
        )
        bundle = output_root / "bundle.json"
        self.assertTrue(bundle.is_file(), "the generated bundle is missing")

        # And the Assistant can read the same answer back by request id.
        status = _execute_agent_tool(
            self.state,
            self.session["id"],
            "optpilot_resource_action_status",
            {"request_id": request_id},
        )
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["data"]["status"], "succeeded")
        self.assertEqual(status["data"]["workspace_id"], workspace["id"])

    def test_without_a_workspace_output_stays_in_studios_own_folder(self) -> None:
        # The browser path passes no Workspace and must keep working.
        request_id = str(uuid.uuid4())
        from optpilot_studio.ui.server import _start_resource_action_run

        payload, _status = _start_resource_action_run(
            self.state,
            {
                "request_id": request_id,
                "resource_uid": self.resource_uid,
                "action_id": "generate",
                "inputs": {"name": "demo"},
            },
        )
        final = self._await(payload["request_id"])
        self.assertEqual(final["status"], "succeeded", final)
        self.assertEqual(final["workspace_id"], "")
        self.assertTrue(
            str(Path(final["result"]["output_root"]).resolve()).startswith(
                str(self.state.runtime_dir.resolve())
            )
        )

    def test_an_unknown_workspace_is_refused(self) -> None:
        from optpilot_studio.ui.server import _start_resource_action_run

        with self.assertRaises(ValueError):
            _start_resource_action_run(
                self.state,
                {
                    "request_id": str(uuid.uuid4()),
                    "resource_uid": self.resource_uid,
                    "action_id": "generate",
                    "workspace_id": "no-such-workspace",
                    "inputs": {"name": "demo"},
                },
            )


if __name__ == "__main__":
    unittest.main()
