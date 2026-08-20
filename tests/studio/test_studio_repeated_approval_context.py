"""Asking to run something again says why it failed last time.

A conversation raised the same approval six times over four minutes. Every
approved run failed for the same reason -- three settings the action needs were
not configured -- and the person saw only a prompt reappearing, with nothing
saying what had gone wrong or that anything had gone wrong at all. The reason
was recorded and readable throughout.

Asking again is not itself the defect: the person may well have fixed the
problem between attempts. Asking again with no account of the last attempt is.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import (
    UiState,
    _append_agent_event_record,
    _create_agent_session,
    _previous_tool_failure_summary,
)


class PreviousFailureTest(unittest.TestCase):
    def _session(self, tmp: Path):
        state = UiState(cwd=tmp, catalog_roots=[], run_roots=[])
        for name in ("sessions_dir", "agent_sessions_dir", "jobs_dir",
                     "workspaces_dir", "runtime_dir"):
            setattr(state, name, tmp / name)
            getattr(state, name).mkdir(parents=True, exist_ok=True)
        state.settings_path = tmp / "settings.json"
        session = _create_agent_session(state, {"title": "approvals"})
        return state, session["id"]

    def _record(self, state, session_id, tool, ok, summary):
        _append_agent_event_record(
            state,
            session_id,
            {
                "id": f"evt_{tool}_{ok}_{summary[:6]}",
                "type": "optpilot_tool_result",
                "created_at": "2026-08-20T06:00:00Z",
                "payload": {"tool": tool, "ok": ok, "summary": summary},
            },
        )

    def test_nothing_is_reported_when_nothing_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, sid = self._session(Path(tmp_dir))
            self.assertEqual(
                _previous_tool_failure_summary(state, sid, "optpilot_x"), ""
            )

    def test_the_last_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, sid = self._session(Path(tmp_dir))
            self._record(state, sid, "optpilot_x", False, "Missing OPENROUTER_API_KEY.")
            self.assertEqual(
                _previous_tool_failure_summary(state, sid, "optpilot_x"),
                "Missing OPENROUTER_API_KEY.",
            )

    def test_a_later_success_clears_it(self) -> None:
        # Otherwise every future approval carries a warning about a problem
        # the person already fixed.
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, sid = self._session(Path(tmp_dir))
            self._record(state, sid, "optpilot_x", False, "Missing key.")
            self._record(state, sid, "optpilot_x", True, "Ran fine.")
            self.assertEqual(
                _previous_tool_failure_summary(state, sid, "optpilot_x"), ""
            )

    def test_another_tool_s_failure_is_not_borrowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state, sid = self._session(Path(tmp_dir))
            self._record(state, sid, "optpilot_other", False, "Unrelated.")
            self.assertEqual(
                _previous_tool_failure_summary(state, sid, "optpilot_x"), ""
            )


if __name__ == "__main__":
    unittest.main()
