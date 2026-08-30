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

import json
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
    _resource_action_review,
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
print("secret=" + os.environ["ACTION_SECRET"])
"""


class AssistantResourceActionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "demo_package"
        self.resource_dir = self.package / "resources" / "demo-generator"
        self.resource_dir.mkdir(parents=True)
        (self.resource_dir / "generate.py").write_text(_GENERATOR, encoding="utf-8")
        self.manifest_path = self.resource_dir / "optpilot.resource.yaml"
        self.manifest_path.write_text(
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
                            "grants": {
                                "network": "enabled",
                                "secretsFromHost": ["ACTION_SECRET"],
                            },
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
        _update_agent_settings(
            self.state,
            {
                "openhands": {"enabled": False},
                "environment": {
                    "set": [
                        {
                            "name": "ACTION_SECRET",
                            "value": "sk-studio-action-audit",
                        }
                    ]
                },
            },
        )
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
        self.assertEqual(actions[0]["command"], ["python", "generate.py"])
        self.assertEqual(actions[0]["network"], "enabled")

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
        approvals = _read_agent_approvals(self.state, self.session["id"])
        self.assertEqual(len(approvals), 1)
        self.assertIn("python generate.py", approvals[0]["summary"])
        self.assertIn("Network: enabled", approvals[0]["summary"])

    def test_setup_commands_are_disclosed_before_approval(self) -> None:
        manifest = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
        manifest["actions"][0]["runtime"] = {
            "sandbox": "process",
            "setup": {
                "steps": [
                    {
                        "uses": "command",
                        "command": ["python", "prepare.py", "--mode", "release"],
                    }
                ]
            },
        }
        self.manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
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

        self.assertFalse(result["ok"])
        approval = _read_agent_approvals(self.state, self.session["id"])[0]
        self.assertIn("Setup: 1. command python prepare.py --mode release", approval["summary"])

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
        self.assertIn(f"workspace={workspace['id']}", approval["targets"])
        self.assertIn(str(Path(workspace["root"]).resolve()), approval["targets"])
        self.assertTrue(
            any(str(target).startswith("output=") for target in approval["targets"]),
            approval["targets"],
        )
        approved = _approve_agent_action(
            self.state, self.session["id"], approval["id"]
        )
        self.assertTrue(approved["result"]["ok"], approved)

        request_id = approved["result"]["data"]["request_id"]
        final = self._await(request_id)
        self.assertEqual(final["status"], "succeeded", final)
        encoded = json.dumps(final, sort_keys=True)
        self.assertNotIn("sk-studio-action-audit", encoded)
        self.assertIn("[REDACTED]", final["result"]["stdout_tail"])

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

    def test_workspace_must_be_attached_and_editable_before_approval(self) -> None:
        unattached = _create_ui_workspace(
            self.state,
            {"title": "Unattached", "root": str(self.root / "unattached")},
        )
        with self.assertRaises(PermissionError):
            _execute_agent_tool(
                self.state,
                self.session["id"],
                "optpilot_resource_action_run",
                {
                    "resource_uid": self.resource_uid,
                    "action_id": "generate",
                    "inputs": {"name": "demo"},
                    "workspace_id": unattached["id"],
                },
            )

        read_only = _create_ui_workspace(
            self.state,
            {
                "title": "Read only",
                "root": str(self.root / "read-only"),
                "mode": "read-only",
            },
        )
        _attach_agent_workspace(
            self.state, self.session["id"], read_only["id"], select=True
        )
        with self.assertRaises(PermissionError):
            _execute_agent_tool(
                self.state,
                self.session["id"],
                "optpilot_resource_action_run",
                {
                    "resource_uid": self.resource_uid,
                    "action_id": "generate",
                    "inputs": {"name": "demo"},
                    "workspace_id": read_only["id"],
                },
            )
        self.assertEqual(_read_agent_approvals(self.state, self.session["id"]), [])

    def test_workspace_output_symlink_is_refused_before_approval(self) -> None:
        workspace = _create_ui_workspace(
            self.state,
            {"title": "Generated", "root": str(self.root / "workspace")},
        )
        _attach_agent_workspace(
            self.state, self.session["id"], workspace["id"], select=True
        )
        outside = self.root / "outside"
        outside.mkdir()
        (Path(workspace["root"]) / "resource-action-output").symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaisesRegex(PermissionError, "must be a real directory"):
            _execute_agent_tool(
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
        self.assertEqual(_read_agent_approvals(self.state, self.session["id"]), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_changed_resource_contract_invalidates_approval(self) -> None:
        requested = _execute_agent_tool(
            self.state,
            self.session["id"],
            "optpilot_resource_action_run",
            {
                "resource_uid": self.resource_uid,
                "action_id": "generate",
                "inputs": {"name": "demo"},
            },
        )
        self.assertFalse(requested["ok"])
        approval = _read_agent_approvals(self.state, self.session["id"])[0]
        (self.resource_dir / "generate.py").write_text(
            _GENERATOR + "\nprint('changed after approval')\n",
            encoding="utf-8",
        )

        approved = _approve_agent_action(
            self.state, self.session["id"], approval["id"]
        )

        self.assertFalse(approved["result"]["ok"], approved)
        self.assertIn("changed after approval", approved["result"]["summary"])
        self.assertEqual(self.state._resource_action_runs, {})

    def test_workspace_root_change_invalidates_approval(self) -> None:
        workspace = _create_ui_workspace(
            self.state,
            {"id": "stable-workspace", "title": "A", "root": str(self.root / "a")},
        )
        _attach_agent_workspace(
            self.state, self.session["id"], workspace["id"], select=True
        )
        requested = _execute_agent_tool(
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
        self.assertFalse(requested["ok"])
        approval = _read_agent_approvals(self.state, self.session["id"])[0]
        replacement = self.root / "b"
        _create_ui_workspace(
            self.state,
            {"id": workspace["id"], "title": "B", "root": str(replacement)},
        )

        approved = _approve_agent_action(
            self.state, self.session["id"], approval["id"]
        )

        self.assertFalse(approved["result"]["ok"], approved)
        self.assertIn("Workspace root changed after approval", approved["result"]["summary"])
        self.assertFalse((replacement / "resource-action-output").exists())

    def test_without_a_workspace_output_stays_in_studios_own_folder(self) -> None:
        # The browser path passes no Workspace and must keep working.
        request_id = str(uuid.uuid4())
        from optpilot_studio.ui.server import _start_resource_action_run
        review = _resource_action_review(
            self.state,
            resource_uid=self.resource_uid,
            action_id="generate",
        )

        payload, _status = _start_resource_action_run(
            self.state,
            {
                "request_id": request_id,
                "resource_uid": self.resource_uid,
                "action_id": "generate",
                "inputs": {"name": "demo"},
                "_approved_action_contract_digest": review[
                    "action_contract_digest"
                ],
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

    def test_session_payloads_carry_live_background_actions(self) -> None:
        # The transcript's live indicator and the Open Work shelf both read
        # this slim list off every session payload; heavy stdout/stderr
        # tails stay on the per-request status endpoint.
        from optpilot_studio.ui.server import (
            _agent_session_by_id,
            _session_background_action_runs,
        )

        session_id = self.session["id"]
        with self.state._lock:
            self.state._resource_action_runs["fake-run"] = {
                "request_id": "fake-run",
                "resource_uid": self.resource_uid,
                "resource_id": "demo-generator",
                "action_id": "generate",
                "workspace_id": "",
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "summary": {"stdout_tail": "x" * 4000},
                "error": None,
                "agent_session_id": session_id,
            }
        try:
            live = _session_background_action_runs(self.state, session_id)
            self.assertEqual([item["request_id"] for item in live], ["fake-run"])
            self.assertEqual(live[0]["status"], "running")
            self.assertNotIn("summary", live[0])
            self.assertNotIn("stdout_tail", live[0])
            payload = _agent_session_by_id(self.state, session_id)
            self.assertEqual(
                [item["request_id"] for item in payload["background_actions"]],
                ["fake-run"],
            )
            other = _session_background_action_runs(self.state, "someone-else")
            self.assertEqual(other, [])
        finally:
            with self.state._lock:
                self.state._resource_action_runs.pop("fake-run", None)

    def test_a_replayed_approved_run_fabricates_no_second_launch_note(self) -> None:
        # Approving a repeat of an already-running request must not append
        # another "Running in the background" note: nothing new started.
        from optpilot_studio.ui.server import _read_agent_messages

        request_id = str(uuid.uuid4())
        for _attempt in range(2):
            started = _execute_agent_tool(
                self.state,
                self.session["id"],
                "optpilot_resource_action_run",
                {
                    "resource_uid": self.resource_uid,
                    "action_id": "generate",
                    "inputs": {"name": "demo"},
                    "request_id": request_id,
                },
            )
            self.assertFalse(started["ok"])
            approvals = [
                item
                for item in _read_agent_approvals(self.state, self.session["id"])
                if item["status"] == "pending"
            ]
            approved = _approve_agent_action(
                self.state, self.session["id"], approvals[0]["id"]
            )
            self.assertTrue(approved["result"]["ok"], approved)
        self._await(request_id)
        notes = [
            message
            for message in _read_agent_messages(self.state, self.session["id"])
            if message.get("title") == "Running in the background"
        ]
        self.assertEqual(len(notes), 1, notes)

    def test_an_unknown_workspace_is_refused(self) -> None:
        from optpilot_studio.ui.server import _start_resource_action_run
        review = _resource_action_review(
            self.state,
            resource_uid=self.resource_uid,
            action_id="generate",
        )

        with self.assertRaises(ValueError):
            _start_resource_action_run(
                self.state,
                {
                    "request_id": str(uuid.uuid4()),
                    "resource_uid": self.resource_uid,
                    "action_id": "generate",
                    "workspace_id": "no-such-workspace",
                    "inputs": {"name": "demo"},
                    "_approved_action_contract_digest": review[
                        "action_contract_digest"
                    ],
                },
            )


if __name__ == "__main__":
    unittest.main()
