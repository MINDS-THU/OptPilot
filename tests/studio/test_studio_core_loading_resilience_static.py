"""Static contracts for bounded, coherent Studio data loading."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
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


class StudioCoreLoadingResilienceStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")

    def test_startup_loads_are_bounded_and_one_generation_finishes(self) -> None:
        load_all = _function_source(self.source, "loadAll")

        self.assertIn("const CORE_REQUEST_TIMEOUT_MS", self.source)
        self.assertIn("const CATALOG_REQUEST_TIMEOUT_MS = 60_000", self.source)
        self.assertIn("const PLATFORM_STATUS_TIMEOUT_MS", self.source)
        self.assertIn("const RUNS_REQUEST_TIMEOUT_MS", self.source)
        self.assertIn("const RUN_DETAIL_REQUEST_TIMEOUT_MS", self.source)
        self.assertIn("const generation = ++state.loadAllGeneration", load_all)
        self.assertIn("await Promise.allSettled([", load_all)
        self.assertIn("if (generation !== state.loadAllGeneration) return", load_all)
        for loader in (
            "loadWorkspace()",
            "loadRuntimeHealth()",
            "loadCodeServerStatus()",
            "loadAgentSettings()",
            "loadCatalogAndCompatibility({ strict: false })",
            "loadUiWorkspaces()",
            "loadStudyDrafts()",
            "loadAgentSessions()",
            "loadRunsAndJobs()",
        ):
            self.assertIn(loader, load_all)

    def test_core_status_and_collection_loaders_ignore_older_responses(self) -> None:
        contracts = {
            "loadWorkspace": "workspace",
            "loadRuntimeHealth": "runtime",
            "loadCodeServerStatus": "codeServer",
            "loadAgentSettings": "agentSettings",
            "loadUiWorkspaces": "uiWorkspaces",
            "loadStudyDrafts": "studyDrafts",
        }
        for function_name, key in contracts.items():
            with self.subTest(function=function_name):
                loader = _function_source(self.source, function_name)
                self.assertIn(f'nextCoreRequest("{key}")', loader)
                self.assertGreaterEqual(
                    loader.count(f'coreRequestIsCurrent("{key}", requestSeq)'),
                    2,
                )
                self.assertIn("timeoutMs:", loader)

        conversations = _function_source(
            self.source, "refreshAgentSessionSummaries"
        )
        guard = "requestSeq !== state.agentSessionSummaryRequestSeq"
        self.assertGreaterEqual(conversations.count(guard), 2)

    def test_collection_payloads_are_validated_before_replacement(self) -> None:
        catalog_validator = _function_source(self.source, "validateCatalogPayload")
        compatibility_validator = _function_source(
            self.source, "validateCompatibilityPayload"
        )
        workspaces = _function_source(self.source, "loadUiWorkspaces")
        drafts = _function_source(self.source, "loadStudyDrafts")
        conversations = _function_source(
            self.source, "refreshAgentSessionSummaries"
        )
        runs = _function_source(self.source, "loadRunsAndJobs")

        for field in ("environments", "methods", "studies", "resources", "sources"):
            self.assertIn(f'"{field}"', catalog_validator)
        self.assertIn('requireArrayField(payload, "pairs"', compatibility_validator)
        self.assertIn(
            'requireArrayField(payload, "workspaces", "Workspace list")', workspaces
        )
        self.assertNotIn("payload.workspaces || []", workspaces)
        self.assertIn(
            'requireArrayField(payload, "drafts", "Study draft list")', drafts
        )
        self.assertNotIn("payload.drafts || []", drafts)
        self.assertIn(
            'requireArrayField(payload, "sessions", "Conversation list")',
            conversations,
        )
        self.assertIn('requireArrayField(runsPayload, "runs", "Run list")', runs)
        self.assertIn("Array.isArray(runsPayload.catalog.items)", runs)
        self.assertIn("state.runs = runsPayload.runs.map", runs)
        self.assertNotIn("runsPayload.runs || []", runs)

    def test_run_requests_time_out_and_release_the_polling_latch(self) -> None:
        runs = _function_source(self.source, "loadRunsAndJobs")
        detail = _function_source(self.source, "loadRunDetail")

        self.assertIn(
            'getJson("/api/runs", { timeoutMs: RUNS_REQUEST_TIMEOUT_MS, conditionalKey: "runs" })',
            runs,
        )
        self.assertIn("state.runsRefreshInFlight = false", runs)
        self.assertIn("} finally {", runs)
        self.assertIn("timeoutMs: RUN_DETAIL_REQUEST_TIMEOUT_MS", detail)
        self.assertIn("state.runDetailRefreshError", detail)

    def test_transient_platform_failures_keep_and_label_last_known_status(self) -> None:
        workspace = _function_source(self.source, "loadWorkspace")
        runtime = _function_source(self.source, "loadRuntimeHealth")
        code_server = _function_source(self.source, "loadCodeServerStatus")
        settings = _function_source(self.source, "loadAgentSettings")
        stale_service = _function_source(
            self.source, "platformServiceWithRefreshState"
        )
        badge = _function_source(self.source, "compactServiceBadge")

        for loader in (workspace, runtime, code_server, settings):
            self.assertIn("markPlatformStatusSuccess", loader)
            self.assertIn("markPlatformStatusFailure", loader)
        self.assertIn("showing the last known status", stale_service)
        self.assertIn("stale: true", stale_service)
        self.assertIn('if (service.stale) return "STALE"', badge)

    def test_dedicated_code_editor_status_wins_over_workspace_snapshot(self) -> None:
        workspace = _function_source(self.source, "loadWorkspace")
        code_server = _function_source(self.source, "loadCodeServerStatus")

        self.assertIn("!state.platformStatusLoaded.codeServer", workspace)
        self.assertIn("state.codeServer = workspace.code_server", workspace)
        self.assertIn("state.codeServer = codeServer", code_server)
        self.assertIn('markPlatformStatusSuccess("codeServer")', code_server)

    def test_timeout_helper_aborts_with_recoverable_message(self) -> None:
        helper = _function_source(self.source, "fetchWithTimeout")

        self.assertIn("new AbortController()", helper)
        self.assertIn("controller.abort()", helper)
        self.assertIn("Studio did not respond in time. Try again.", helper)
        self.assertIn("window.clearTimeout(timeout)", helper)


if __name__ == "__main__":
    unittest.main()
