"""The Open work approval card must report the true pending count.

The session summary describes one list with three overlapping fields:
``pending_approval_count`` is the whole pending set, ``active_approval_ids``
holds the single request currently offered, and ``queued_approval_count`` is
the remaining tail. A client that adds the pending and queued counts
double-counts the queue, which is exactly how the shelf once rendered
"5 pending approvals" for three of them. These tests pin the server contract
and then run the real client projection over the real server payload.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _append_agent_message,
    _create_agent_session,
    _list_agent_session_summaries,
    _upsert_agent_approval,
)


_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static" / "app.js"
_NODE = shutil.which("node")


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


def _session_summary_with_pending_approvals(count: int) -> dict:
    """Build a real session summary carrying ``count`` pending approvals."""

    with tempfile.TemporaryDirectory() as tmp_dir:
        state = UiState(cwd=Path(tmp_dir), catalog_roots=[], run_roots=[])
        session = _create_agent_session(state, {"title": "Scheduling"})
        _append_agent_message(
            state,
            session["id"],
            {"role": "user", "title": "User", "content": "Improve throughput."},
        )
        for index in range(count):
            _upsert_agent_approval(
                state,
                session["id"],
                {
                    "id": f"approval-{index}",
                    "status": "pending",
                    "kind": "tool",
                    "title": f"Approve step {index}",
                },
            )
        summaries = _list_agent_session_summaries(state)
        return next(item for item in summaries if item["id"] == session["id"])


def _open_work_items(session_summaries: list) -> list:
    """Run the shipped buildOpenWorkItems() over the given session summaries."""

    projection = _function_source(_APP.read_text(encoding="utf-8"), "buildOpenWorkItems")
    state = {
        "interfaceLaunch": None,
        "studyLaunch": None,
        "runs": [],
        "openWorkErrors": {},
        "agentSessions": session_summaries,
    }
    # Stubs stand in for the collaborators the approval branch does not need;
    # the two assistant helpers mirror their real one-line implementations.
    harness = f"""
"use strict";
const state = {json.dumps(state)};
function isActiveInterfaceLaunch() {{ return false; }}
function interfaceOutputsNeedAttention() {{ return false; }}
function activeInterfaceStatusText() {{ return ""; }}
function isViewingActiveInterface() {{ return false; }}
function studyLaunchIsTerminal() {{ return false; }}
function openWorkTimestamp() {{ return 0; }}
function runStatus(run) {{ return String(run && run.status || ""); }}
function canonicalRunId(run) {{ return String(run && run.id || ""); }}
function runPlannedWork() {{ return ""; }}
function assistantSessionStatus(session) {{
  return session && (session.effective_status || session.status) || "";
}}
function assistantSessionLabel(session) {{
  return String(session && session.title || "Conversation");
}}
{projection}
process.stdout.write(JSON.stringify(buildOpenWorkItems()));
"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        script = Path(tmp_dir) / "open-work-projection.js"
        script.write_text(harness, encoding="utf-8")
        completed = subprocess.run(
            [str(_NODE), str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise AssertionError(
            f"buildOpenWorkItems() harness failed: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


class OpenWorkApprovalCountContractTests(unittest.TestCase):
    def test_queued_count_is_the_tail_of_the_pending_count(self) -> None:
        summary = _session_summary_with_pending_approvals(3)

        self.assertEqual(summary["pending_approval_count"], 3)
        self.assertEqual(len(summary["active_approval_ids"]), 1)
        self.assertEqual(summary["queued_approval_count"], 2)
        # The fields overlap by design: pending covers the active request plus
        # the queued tail. Clients must not add pending and queued together.
        self.assertEqual(
            summary["pending_approval_count"],
            len(summary["active_approval_ids"]) + summary["queued_approval_count"],
        )

    def test_a_single_pending_approval_leaves_an_empty_queue(self) -> None:
        summary = _session_summary_with_pending_approvals(1)

        self.assertEqual(summary["pending_approval_count"], 1)
        self.assertEqual(summary["queued_approval_count"], 0)
        self.assertEqual(summary["effective_status"], "awaiting_user_approval")


@unittest.skipUnless(_NODE, "node is required to evaluate the client projection")
class OpenWorkApprovalCardArithmeticTests(unittest.TestCase):
    def _approval_card(self, pending: int) -> dict:
        summary = _session_summary_with_pending_approvals(pending)
        items = _open_work_items([summary])
        approvals = [item for item in items if item["kind"] == "approval"]
        self.assertEqual(len(approvals), 1, "expected exactly one approval card")
        return approvals[0]

    def test_three_pending_approvals_render_as_three(self) -> None:
        card = self._approval_card(3)

        self.assertEqual(card["subtitle"], "3 pending approvals · Click to review")
        self.assertEqual(card["section"], "Needs attention")

    def test_two_pending_approvals_render_as_two(self) -> None:
        card = self._approval_card(2)

        self.assertEqual(card["subtitle"], "2 pending approvals · Click to review")

    def test_a_single_pending_approval_keeps_the_singular_wording(self) -> None:
        card = self._approval_card(1)

        self.assertEqual(card["subtitle"], "Pending approval · Click to review")

    def test_a_session_without_pending_approvals_has_no_card(self) -> None:
        summary = _session_summary_with_pending_approvals(0)
        items = _open_work_items([summary])

        self.assertEqual(summary["pending_approval_count"], 0)
        self.assertEqual([item for item in items if item["kind"] == "approval"], [])


if __name__ == "__main__":
    unittest.main()
