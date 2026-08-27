"""A conversation advances with nobody watching it.

Until now a turn only moved while a browser tab polled it. Reproduced live:
the agent-server finished a run in 23 seconds -- the model called the finish
tool and stopped at an approval gate -- and Studio's session sat on
"waiting_for_agent" for 4.6 hours, because the only open tab was in the
background where the page stops polling. One manual sync advanced it
instantly. Background-action wake-ups had the same exposure: the outcome is
posted into the conversation and the session flipped to waiting, but the
model's follow-up was never harvested.

Studio now runs one tick of its own. It reads the session index (never a
session payload, which writes as a side effect), takes each session's
operation lock without blocking so it never queues behind a person's own
request, and syncs the few oldest busy sessions. Sessions awaiting a person,
idle, errored, or archived are not its business.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from optpilot_studio.ui.server import (
    UiState,
    _agent_session_operation_lock,
    _agent_tick_candidate_session_ids,
    _require_agent_session,
    _run_agent_session_tick_cycle,
    _start_agent_session_tick,
    _upsert_agent_session,
)


def _state(tmp: Path, *, interval: float = 0.0) -> UiState:
    state = UiState(
        cwd=tmp,
        catalog_roots=[],
        run_roots=[],
        agent_tick_interval_seconds=interval,
    )
    for name in ("sessions_dir", "agent_sessions_dir", "jobs_dir", "workspaces_dir", "runtime_dir"):
        setattr(state, name, tmp / name)
        getattr(state, name).mkdir(parents=True, exist_ok=True)
    state.settings_path = tmp / "settings.json"
    state.agent_adapter = mock.Mock()
    state.agent_adapter.status.return_value = {
        "dispatch": "openhands_http",
        "connected": True,
    }
    return state


def _session(
    state: UiState,
    session_id: str,
    *,
    status: str = "waiting_for_agent",
    conversation: Optional[str] = "conv-1",
    updated_at: str = "2026-08-27T04:00:00Z",
    archived: bool = False,
) -> None:
    record = {
        "id": session_id,
        "title": session_id,
        "status": status,
        "updated_at": updated_at,
        "created_at": updated_at,
        "attached_workspace_ids": [],
    }
    if conversation:
        record["openhands_conversation_id"] = conversation
    if archived:
        record["archived"] = True
    _upsert_agent_session(state, record)


class TickIsOffUnlessAskedFor(unittest.TestCase):
    """Hundreds of tests build a UiState; none may leak a polling thread."""

    def test_a_plain_state_starts_no_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            self.addCleanup(state.close_coordination)
            self.assertEqual(state._agent_tick_threads, {})
            self.assertFalse(_start_agent_session_tick(state))

    def test_an_interval_without_the_supervisor_claim_still_starts_nothing(self) -> None:
        # Only the instance that owns this directory may drive its sessions.
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp), interval=5.0)
            self.addCleanup(state.close_coordination)
            self.assertIsNone(state._runtime_supervisor_claim)
            self.assertFalse(_start_agent_session_tick(state))
            self.assertEqual(state._agent_tick_threads, {})


class SelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = _state(Path(temporary.name))
        self.addCleanup(self.state.close_coordination)

    def test_only_busy_conversations_are_candidates(self) -> None:
        _session(self.state, "as_waiting", status="waiting_for_agent")
        _session(self.state, "as_running", status="running")
        _session(self.state, "as_idle", status="idle")
        _session(self.state, "as_approval", status="awaiting_user_approval")
        _session(self.state, "as_error", status="error")
        _session(self.state, "as_no_conv", status="waiting_for_agent", conversation=None)
        _session(self.state, "as_archived", status="waiting_for_agent", archived=True)
        self.assertEqual(
            set(_agent_tick_candidate_session_ids(self.state, now=100.0)),
            {"as_waiting", "as_running"},
        )

    def test_the_longest_stalled_go_first_and_the_cycle_is_capped(self) -> None:
        for index in range(7):
            _session(
                self.state,
                f"as_{index}",
                updated_at=f"2026-08-27T04:0{index}:00Z",
            )
        picked = _agent_tick_candidate_session_ids(self.state, now=100.0)
        self.assertEqual(picked, ["as_0", "as_1", "as_2", "as_3"])

    def test_a_session_in_backoff_waits_its_turn(self) -> None:
        _session(self.state, "as_backed_off")
        self.state._agent_tick_backoff["as_backed_off"] = (500.0, 20.0)
        self.assertEqual(_agent_tick_candidate_session_ids(self.state, now=100.0), [])
        self.assertEqual(
            _agent_tick_candidate_session_ids(self.state, now=600.0),
            ["as_backed_off"],
        )


class CycleTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = _state(Path(temporary.name), interval=5.0)
        self.addCleanup(self.state.close_coordination)

    def test_the_reported_stall_is_advanced_with_no_client(self) -> None:
        # The live shape: the agent-server already finished; nothing polled.
        _session(self.state, "as_stalled")
        with mock.patch(
            "optpilot_studio.ui.server._sync_agent_session"
        ) as sync:
            sync.return_value = {"id": "as_stalled", "status": "idle"}
            self.assertEqual(
                _run_agent_session_tick_cycle(self.state, now=100.0), ["as_stalled"]
            )
        self.assertEqual(sync.call_args.kwargs["poll_seconds"], 0.75)
        self.assertNotIn("as_stalled", self.state._agent_tick_backoff)

    def test_a_session_a_person_is_using_is_left_alone(self) -> None:
        _session(self.state, "as_busy")
        holder_has_lock = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with _agent_session_operation_lock(self.state, "as_busy"):
                holder_has_lock.set()
                release.wait(5.0)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 5.0)
        self.addCleanup(release.set)
        self.assertTrue(holder_has_lock.wait(5.0))
        with mock.patch("optpilot_studio.ui.server._sync_agent_session") as sync:
            self.assertEqual(_run_agent_session_tick_cycle(self.state, now=100.0), [])
            sync.assert_not_called()
        release.set()
        holder.join(5.0)
        with mock.patch("optpilot_studio.ui.server._sync_agent_session") as sync:
            sync.return_value = {"id": "as_busy", "status": "idle"}
            self.assertEqual(
                _run_agent_session_tick_cycle(self.state, now=100.0), ["as_busy"]
            )

    def test_an_unreachable_agent_server_costs_one_probe(self) -> None:
        _session(self.state, "as_stalled")
        self.state.agent_adapter.status.return_value = {
            "dispatch": "openhands_http",
            "connected": False,
        }
        with mock.patch("optpilot_studio.ui.server._sync_agent_session") as sync:
            self.assertEqual(_run_agent_session_tick_cycle(self.state, now=100.0), [])
            sync.assert_not_called()

    def test_a_failing_sync_never_ends_the_thread(self) -> None:
        # Deliberately not timeout-like, so it escapes the sync's own handling.
        _session(self.state, "as_broken")
        with mock.patch(
            "optpilot_studio.ui.server._sync_agent_session",
            side_effect=RuntimeError("HTTP 502 from http://127.0.0.1:8781"),
        ):
            self.assertEqual(_run_agent_session_tick_cycle(self.state, now=100.0), [])
        self.assertIn("as_broken", self.state._agent_tick_backoff)

    def test_a_long_turn_keeps_the_steady_cadence(self) -> None:
        # A model thinking for minutes yields "still running" every cycle.
        # Backing off for that left the tick asleep at the moment the turn
        # finally finished -- observed live, the session sat on
        # waiting_for_agent while its conversation had already finished.
        _session(self.state, "as_thinking")
        with mock.patch("optpilot_studio.ui.server._sync_agent_session") as sync:
            sync.return_value = {"id": "as_thinking", "status": "waiting_for_agent"}
            for _ in range(4):
                self.assertEqual(
                    _run_agent_session_tick_cycle(self.state, now=100.0),
                    ["as_thinking"],
                )
        self.assertNotIn("as_thinking", self.state._agent_tick_backoff)

    def test_a_failing_session_backs_off_and_recovery_clears_it(self) -> None:
        _session(self.state, "as_stuck")
        with mock.patch(
            "optpilot_studio.ui.server._sync_agent_session",
            side_effect=RuntimeError("HTTP 502"),
        ):
            _run_agent_session_tick_cycle(self.state, now=100.0)
            first_due, first_delay = self.state._agent_tick_backoff["as_stuck"]
            _run_agent_session_tick_cycle(self.state, now=first_due)
            _second_due, second_delay = self.state._agent_tick_backoff["as_stuck"]
        self.assertGreater(second_delay, first_delay)
        self.assertLessEqual(second_delay, 300.0)
        with mock.patch("optpilot_studio.ui.server._sync_agent_session") as sync:
            sync.return_value = {"id": "as_stuck", "status": "idle"}
            _run_agent_session_tick_cycle(self.state, now=100_000.0)
        self.assertNotIn("as_stuck", self.state._agent_tick_backoff)


if __name__ == "__main__":
    unittest.main()
