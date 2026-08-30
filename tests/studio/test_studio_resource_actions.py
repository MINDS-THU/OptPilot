"""Studio surface for headless resource actions (U1b over F4)."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

import optpilot_studio.ui.server as studio_server
from optpilot.realm.errors import RealmConflict
from optpilot_studio.ui.server import (
    UiState,
    _catalog_payload,
    _handler_factory,
    _resource_action_review,
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
_NODE = shutil.which("node")


def _can_bind_loopback() -> bool:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
    except OSError:
        return False
    finally:
        listener.close()
    return True


_LOOPBACK_TCP_BIND_AVAILABLE = _can_bind_loopback()


def _function_source(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^(?:async\s+)?function\s+{re.escape(name)}\s*\(", source
    )
    if match is None:
        raise AssertionError(f"JavaScript function {name!r} was not found")
    successor = re.search(
        r"(?m)^(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
        source[match.end() :],
    )
    end = len(source) if successor is None else match.end() + successor.start()
    return source[match.start() : end]


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
                            "grants": {"network": "enabled"},
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

    def _action_digest(self, resource_uid: str) -> str:
        review = _resource_action_review(
            self.state,
            resource_uid=resource_uid,
            action_id="generate",
        )
        return str(review["action_contract_digest"])

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
            "_approved_action_contract_digest": self._action_digest(entry["uid"]),
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

        with self.assertRaisesRegex(RealmConflict, "different execution request"):
            _start_resource_action_run(
                self.state,
                {**request, "inputs": {"name": "Grace"}},
            )

    def test_invalid_inputs_fail_the_run_with_the_core_error(self) -> None:
        resources = _catalog_payload(self.state)["resources"]
        entry = next(item for item in resources if item["id"] == "demo-generator")
        request = {
            "request_id": "92345678-1234-4234-8234-123456789abc",
            "resource_uid": entry["uid"],
            "action_id": "generate",
            "inputs": {},
            "_approved_action_contract_digest": self._action_digest(entry["uid"]),
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
                    "_approved_action_contract_digest": "0" * 64,
                },
            )
        with self.assertRaises(KeyError):
            _resource_action_run_status(
                self.state, "a2345678-1234-4234-8234-123456789abc"
            )

    def test_review_returns_the_exact_action_and_tree_bound_digest(self) -> None:
        entry = next(
            item
            for item in _catalog_payload(self.state)["resources"]
            if item["id"] == "demo-generator"
        )
        review = _resource_action_review(
            self.state, resource_uid=entry["uid"], action_id="generate"
        )
        self.assertEqual(
            review["schema"], "optpilot.studio-resource-action-review.v1"
        )
        self.assertEqual(review["action"], entry["raw_config"]["actions"][0])
        self.assertRegex(review["action_contract_digest"], r"^[0-9a-f]{64}$")

    @unittest.skipUnless(
        _LOOPBACK_TCP_BIND_AVAILABLE, "sandbox denies loopback TCP bind"
    )
    def test_review_http_endpoint_returns_the_server_issued_contract(self) -> None:
        entry = next(
            item
            for item in _catalog_payload(self.state)["resources"]
            if item["id"] == "demo-generator"
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(self.state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            query = urlencode(
                {"resource_uid": entry["uid"], "action_id": "generate"}
            )
            with urlopen(
                f"{base_url}/api/resource-actions/review?{query}",
                timeout=5,
            ) as response:
                self.assertEqual(response.status, HTTPStatus.OK)
                payload = json.loads(response.read().decode("utf-8"))

            with urlopen(f"{base_url}/api/security-context", timeout=5) as response:
                security = json.loads(response.read().decode("utf-8"))
            request_id = "d2345678-1234-4234-8234-123456789abc"
            run_request = Request(
                f"{base_url}/api/resource-actions/run",
                data=json.dumps(
                    {
                        "request_id": request_id,
                        "resource_uid": entry["uid"],
                        "action_id": "generate",
                        "inputs": {"name": "HTTP"},
                        "_approved_action_contract_digest": payload[
                            "action_contract_digest"
                        ],
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": base_url,
                    security["csrf_header"]: security["csrf_token"],
                },
                method="POST",
            )
            with urlopen(run_request, timeout=5) as response:
                self.assertEqual(response.status, HTTPStatus.ACCEPTED)
            self.assertEqual(self._await_run(request_id)["status"], "succeeded")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(payload["resource_uid"], entry["uid"])
        self.assertEqual(payload["action"], entry["raw_config"]["actions"][0])
        self.assertEqual(payload["action_contract_digest"], self._action_digest(entry["uid"]))

    def test_run_requires_review_and_rejects_a_changed_executable_tree(self) -> None:
        entry = next(
            item
            for item in _catalog_payload(self.state)["resources"]
            if item["id"] == "demo-generator"
        )
        base = {
            "request_id": "b2345678-1234-4234-8234-123456789abc",
            "resource_uid": entry["uid"],
            "action_id": "generate",
            "inputs": {"name": "Ada"},
        }
        with self.assertRaisesRegex(ValueError, "Review the current Resource action"):
            _start_resource_action_run(self.state, base)

        digest = self._action_digest(entry["uid"])
        (self.package / "resources" / "demo-generator" / "generate.py").write_text(
            "raise SystemExit('changed after review')\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RealmConflict, "changed after approval"):
            _start_resource_action_run(
                self.state,
                {**base, "_approved_action_contract_digest": digest},
            )

    def test_concurrent_insertions_cannot_reuse_one_terminal_capacity_slot(self) -> None:
        entry = next(
            item
            for item in _catalog_payload(self.state)["resources"]
            if item["id"] == "demo-generator"
        )
        digest = self._action_digest(entry["uid"])
        with self.state._lock:
            for index in range(studio_server._MAX_RESOURCE_ACTION_RUNS - 1):
                self.state._resource_action_runs[f"running-{index}"] = {
                    "status": "running"
                }
            self.state._resource_action_runs["one-terminal-slot"] = {
                "status": "failed"
            }

        callers_ready = threading.Barrier(2)
        release_action = threading.Event()
        original_prepare = studio_server._prepare_resource_action_execution
        responses: list[dict] = []
        errors: list[BaseException] = []

        def prepare_after_both_prechecks(*args, **kwargs):
            callers_ready.wait(timeout=5)
            return original_prepare(*args, **kwargs)

        def blocked_run(*args, **kwargs):
            if not release_action.wait(timeout=10):
                raise RuntimeError("test action was not released")
            return {
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration_seconds": 0.0,
                "outputs": [],
                "output_root": str(kwargs["output_root"]),
                "stdout_tail": "",
                "stderr_tail": "",
                "error": None,
            }

        def submit(request_id: str) -> None:
            try:
                response, _status = _start_resource_action_run(
                    self.state,
                    {
                        "request_id": request_id,
                        "resource_uid": entry["uid"],
                        "action_id": "generate",
                        "inputs": {"name": "Ada"},
                        "_approved_action_contract_digest": digest,
                    },
                )
                responses.append(response)
            except BaseException as error:  # preserve the worker assertion
                errors.append(error)

        with mock.patch.object(
            studio_server,
            "_prepare_resource_action_execution",
            side_effect=prepare_after_both_prechecks,
        ), mock.patch.object(
            studio_server, "run_resource_action", side_effect=blocked_run
        ):
            callers = [
                threading.Thread(
                    target=submit,
                    args=(f"c2345678-1234-4234-8234-123456789ab{index}",),
                )
                for index in range(2)
            ]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=10)
                self.assertFalse(caller.is_alive(), "concurrent submitter stalled")

            self.assertEqual(len(responses), 1, (responses, errors))
            self.assertEqual(len(errors), 1, (responses, errors))
            self.assertIsInstance(errors[0], ValueError)
            self.assertIn("Too many concurrent", str(errors[0]))
            with self.state._lock:
                self.assertEqual(
                    len(self.state._resource_action_runs),
                    studio_server._MAX_RESOURCE_ACTION_RUNS,
                )

            release_action.set()
            self._await_run(str(responses[0]["request_id"]))

    def test_client_renders_action_forms_and_run_controls(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("function resourceActionsPanel(", source)
        self.assertIn("data-run-resource-action", source)
        self.assertIn("/api/resource-actions/run", source)
        self.assertIn("resourceActionsPanel(item)", source)
        self.assertIn("bindResourceActionControls(item)", source)
        self.assertIn("<h3>Headless actions</h3>", source)
        self.assertIn("Declared in this Resource's YAML under <code>actions</code>", source)
        self.assertIn("function resourceActionSetupDescriptions(action)", source)
        self.assertIn("<dt>Setup</dt>", source)
        self.assertIn("`Setup: ${description}`", source)
        self.assertIn("/api/resource-actions/review?resource_uid=", source)
        self.assertIn("validatedResourceActionReview(payload, uid, actionId)", source)
        self.assertIn("The Resource action changed after review.", source)
        run_handler = _function_source(source, "runResourceActionFromCatalog")
        self.assertIn("const runRequest = resourceActionRunRequest(", run_handler)
        self.assertIn("recoveryRequest: runRequest", run_handler)
        self.assertIn("confirming_submission: true", run_handler)

    @unittest.skipUnless(_NODE, "node is required to evaluate the client request builder")
    def test_client_run_request_binds_the_reviewed_contract_digest(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        request_builder = _function_source(source, "resourceActionRunRequest")
        digest = "a" * 64
        harness = f"""
"use strict";
{request_builder}
const request = resourceActionRunRequest(
  "82345678-1234-4234-8234-123456789abc",
  "resource-ref",
  "generate",
  {{name: "Ada"}},
  {{action_contract_digest: {json.dumps(digest)}}},
);
process.stdout.write(JSON.stringify(request));
"""
        completed = subprocess.run(
            [str(_NODE), "-e", harness],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "request_id": "82345678-1234-4234-8234-123456789abc",
                "resource_uid": "resource-ref",
                "action_id": "generate",
                "inputs": {"name": "Ada"},
                "_approved_action_contract_digest": digest,
            },
        )

    @unittest.skipUnless(_NODE, "node is required to evaluate the client request builder")
    def test_client_run_request_refuses_a_missing_review_digest(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        request_builder = _function_source(source, "resourceActionRunRequest")
        harness = f"""
"use strict";
{request_builder}
try {{
  resourceActionRunRequest("request", "resource", "generate", {{}}, {{}});
  process.exitCode = 2;
}} catch (error) {{
  process.stdout.write(String(error.message || error));
}}
"""
        completed = subprocess.run(
            [str(_NODE), "-e", harness],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout, "Review this Resource action before running it."
        )

    @unittest.skipUnless(_NODE, "node is required to evaluate client recovery")
    def test_client_ambiguous_submission_replays_the_exact_same_request(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")
        poller = _function_source(source, "pollResourceActionRun")
        digest = "b" * 64
        request_id = "f2345678-1234-4234-8234-123456789abc"
        request = {
            "request_id": request_id,
            "resource_uid": "resource-ref",
            "action_id": "generate",
            "inputs": {"name": "Ada"},
            "_approved_action_contract_digest": digest,
        }
        harness = f"""
"use strict";
{poller}
const expectedRequest = {json.dumps(request)};
const calls = [];
const state = {{
  resourceActionRuns: new Map([["resource-ref::generate", {{
    status: "running",
    request_id: expectedRequest.request_id,
    confirming_submission: true,
  }}]]),
}};
global.setTimeout = (callback) => {{ callback(); return 1; }};
async function getJson() {{
  const error = new Error("not recorded yet");
  error.status = 404;
  throw error;
}}
async function postJson(url, payload, options) {{
  calls.push({{url, payload, options, sameObject: payload === expectedRequest}});
  return {{status: "succeeded", request_id: payload.request_id, result: {{ok: true}}}};
}}
function renderCatalog() {{}}
function boundedPublicActionError(error, fallback) {{ return fallback; }}
async function loadCatalogAndCompatibility() {{}}
(async () => {{
  await pollResourceActionRun(
    "resource-ref::generate",
    expectedRequest.request_id,
    {{recoveryRequest: expectedRequest}},
  );
  process.stdout.write(JSON.stringify({{
    calls,
    final: state.resourceActionRuns.get("resource-ref::generate"),
  }}));
}})().catch((error) => {{
  process.stderr.write(String(error && error.stack || error));
  process.exitCode = 1;
}});
"""
        completed = subprocess.run(
            [str(_NODE), "-e", harness],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(len(result["calls"]), 1, result)
        self.assertTrue(result["calls"][0]["sameObject"], result)
        self.assertEqual(result["calls"][0]["url"], "/api/resource-actions/run")
        self.assertEqual(result["calls"][0]["payload"], request)
        self.assertEqual(result["final"]["status"], "succeeded")
        self.assertEqual(result["final"]["request_id"], request_id)


if __name__ == "__main__":
    unittest.main()
