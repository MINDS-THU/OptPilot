"""Static contracts for truthful, same-launch Interface recovery."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = (
    _ROOT
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)


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


class StudioInterfaceReconnectStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_status_fetch_retries_the_same_launch_with_a_bound(self) -> None:
        fetch = _function_source(
            self.source, "fetchInterfaceLaunchStatusWithRecovery"
        )

        self.assertIn("INTERFACE_LAUNCH_RECONNECT_LIMIT = 5", self.source)
        self.assertIn("INTERFACE_LAUNCH_POLL_TIMEOUT_MS = 10_000", self.source)
        self.assertIn("timeoutMs: INTERFACE_LAUNCH_POLL_TIMEOUT_MS", fetch)
        self.assertIn("state.interfaceLaunch.key === launchKey", fetch)
        self.assertIn("state.interfaceLaunch.launch_id", fetch)
        self.assertIn("[404, 410].includes(status)", fetch)
        self.assertIn('connection_status: unavailable ? "unavailable" : "reconnecting"', fetch)
        self.assertIn("INTERFACE_LAUNCH_RECONNECT_LIMIT", fetch)
        self.assertIn("await sleep(1200)", fetch)
        self.assertIn("interfaceConnectionUnavailable = true", fetch)

    def test_every_launch_status_path_uses_the_shared_recovery(self) -> None:
        resume = _function_source(self.source, "resumeStoredInterfaceLaunch")
        catalog = _function_source(self.source, "pollComponentInterfaceLaunch")
        workspace = _function_source(self.source, "pollWorkspaceInterfaceLaunch")

        for body in (resume, catalog, workspace):
            self.assertIn(
                "fetchInterfaceLaunchStatusWithRecovery(launchKey, launchId)",
                body,
            )
        for body in (catalog, workspace):
            self.assertNotIn("if (!readyObserved) throw error", body)
            self.assertNotIn("await getJson(`/api/interface-launches/", body)

    def test_reconnect_resumes_in_place_instead_of_starting_another_runtime(self) -> None:
        resume = _function_source(self.source, "resumeInterfaceLaunchPolling")

        self.assertIn("launch.launch_id", resume)
        self.assertIn("pollWorkspaceInterfaceLaunch(launchKey, launchId)", resume)
        self.assertIn("pollComponentInterfaceLaunch(launchKey, launchId)", resume)
        self.assertNotIn("launchComponentInterface", resume)
        self.assertNotIn("launchWorkspaceInterface", resume)

    def test_connection_state_is_persisted_separately_from_runtime_status(self) -> None:
        persist = _function_source(self.source, "persistActiveInterfaceLaunch")
        handler = _function_source(self.source, "handleInterfaceLaunchPollingError")
        session = _function_source(self.source, "launchInterfaceSessionModel")

        self.assertIn("connection_status", persist)
        self.assertIn("reconnect_attempts", persist)
        self.assertIn("connection_error", persist)
        self.assertIn("if (error && error.interfaceConnectionUnavailable)", handler)
        self.assertIn('const status = String(launch.status || "queued")', session)
        self.assertIn("const connectionStatus", session)
        self.assertIn("resumeInterfaceLaunchPolling(launch)", session)

    def test_catalog_and_workspace_surfaces_offer_reconnect(self) -> None:
        compact = _function_source(self.source, "compactInterfaceLaunchStatus")
        catalog = _function_source(
            self.source, "bindComponentInterfaceLaunchControls"
        )
        workspace = _function_source(
            self.source, "bindWorkspaceInterfaceLaunchControls"
        )

        self.assertIn("Reconnecting to ${label}", compact)
        self.assertIn("Connection to ${label} needs attention", compact)
        self.assertIn('class="primary-button interface-reconnect"', compact)
        for body in (catalog, workspace):
            self.assertIn('.querySelector(".interface-reconnect")', body)
            self.assertIn("resumeInterfaceLaunchPolling()", body)


if __name__ == "__main__":
    unittest.main()
