"""Focused contracts for coherent Run list/detail navigation."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "studio" / "src" / "optpilot_studio" / "ui" / "static"
_APP = _STATIC / "app.js"
_STYLES = _STATIC / "styles.css"


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


class StudioRunSelectionNavigationStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP.read_text(encoding="utf-8")
        cls.styles = _STYLES.read_text(encoding="utf-8")

    def test_new_selection_is_visible_before_the_detail_request(self) -> None:
        load = _function_source(self.source, "loadRunDetail")

        select = load.index("state.selectedRunId = runId;")
        clear_stale = load.index("if (showLoadingState) state.selectedRun = null;")
        render_list = load.index("if (!options.skipListRender) renderRuns();")
        render_detail = load.index("if (showLoadingState) renderRunDetail();")
        request = load.index("const detail = await getJson(")
        self.assertLess(select, clear_stale)
        self.assertLess(clear_stale, render_list)
        self.assertLess(render_list, request)
        self.assertLess(render_detail, request)

    def test_only_the_current_request_can_commit_success_or_failure(self) -> None:
        load = _function_source(self.source, "loadRunDetail")
        guard = (
            "requestSeq !== state.runDetailRequestSeq "
            "|| state.selectedRunId !== runId"
        )

        self.assertGreaterEqual(load.count(guard), 3)
        catch = load[load.index("} catch (error) {") :]
        self.assertIn(f"if ({guard}) return null;", catch)
        self.assertIn("if (showLoadingState) {", catch)
        self.assertIn("state.selectedRun = null;", catch)
        self.assertIn("state.runDetailError =", catch)
        self.assertIn("renderRunDetail();", catch)

    def test_detail_pane_has_loading_error_and_retry_states(self) -> None:
        render = _function_source(self.source, "renderRunDetail")

        self.assertIn('role="status" aria-live="polite"', render)
        self.assertIn("Loading Run…", render)
        self.assertIn('role="alert"', render)
        self.assertIn("This Run could not be loaded.", render)
        self.assertIn('class="ghost-button retry-run-detail"', render)
        self.assertIn(
            "loadRunDetail(state.selectedRunId, { keepTab: true }).catch(() => {});",
            render,
        )
        self.assertIn(
            "canonicalRunId(state.selectedRun.run) === state.selectedRunId",
            render,
        )

    def test_selected_row_exposes_selection_and_loading_state(self) -> None:
        row = _function_source(self.source, "runRow")

        self.assertIn('aria-current="${rowKey === state.selectedRunId ? "true" : "false"}"', row)
        self.assertIn('class="run-row-load-status" role="status"', row)
        self.assertIn("state.runDetailLoadingRunId === rowKey", row)
        self.assertIn(".run-row-load-status", self.styles)
        self.assertIn(".run-detail-load-state", self.styles)

    def test_filters_clear_a_selected_run_that_is_no_longer_visible(self) -> None:
        reconcile = _function_source(
            self.source, "reconcileRunSelectionWithVisibleRows"
        )
        render = _function_source(self.source, "renderRuns")

        self.assertIn(
            "runs.some((run) => canonicalRunId(run) === state.selectedRunId)",
            reconcile,
        )
        self.assertIn("state.selectedRunId = null;", reconcile)
        self.assertIn("state.selectedRun = null;", reconcile)
        self.assertIn("state.routedCandidateId = null;", reconcile)
        self.assertIn("state.runDetailRequestSeq += 1;", reconcile)
        self.assertIn("syncStudioRoute();", reconcile)
        self.assertIn(
            "const selectionCleared = reconcileRunSelectionWithVisibleRows(runs);",
            render,
        )
        self.assertIn("if (selectionCleared) renderRunDetail();", render)

    def test_exact_route_resolution_is_not_gated_by_the_first_run_list_page(self) -> None:
        application = _function_source(self.source, "applyStudioRoute")

        self.assertNotIn("state.runs.some", application)
        self.assertNotIn("state.runsLoaded", application)
        self.assertGreaterEqual(
            application.count("state.runRouteResolutionPendingId = route.runId;"),
            2,
        )
        self.assertIn(
            "loadRunDetail(route.runId, { keepTab: true, skipListRender: true, fromRoute: true })",
            application,
        )

    def test_pending_exact_route_survives_list_reconciliation(self) -> None:
        reconcile = _function_source(
            self.source, "reconcileRunSelectionWithVisibleRows"
        )

        pending_guard = (
            "if (state.runRouteResolutionPendingId === state.selectedRunId) "
            "return false;"
        )
        self.assertIn(pending_guard, reconcile)
        self.assertLess(
            reconcile.index(pending_guard),
            reconcile.index("state.selectedRunId = null;"),
        )

    def test_exact_detail_is_preserved_across_paginated_list_refreshes(self) -> None:
        loading = _function_source(self.source, "loadRunsAndJobs")
        preserve = _function_source(self.source, "preserveSelectedRunSummary")
        merge = _function_source(self.source, "mergeExactRunSummary")
        detail = _function_source(self.source, "loadRunDetail")

        self.assertIn(
            "state.runs = preserveSelectedRunSummary(state.runs);", loading
        )
        self.assertIn("detailRunId !== state.selectedRunId", preserve)
        self.assertIn("return [detailRun, ...rows];", preserve)
        self.assertIn("state.runs = [detailRun, ...state.runs];", merge)
        self.assertIn("mergeExactRunSummary(detail.run)", detail)
        self.assertIn(
            "if (state.runRouteResolutionPendingId === runId) "
            "state.runRouteResolutionPendingId = null;",
            detail,
        )

    def test_only_an_exact_404_settles_a_missing_run_route(self) -> None:
        load = _function_source(self.source, "loadRunDetail")
        settle = _function_source(self.source, "settleMissingRunRoute")
        get_json = _function_source(self.source, "getJson")

        self.assertIn("error.status = response.status;", get_json)
        self.assertIn(
            "Number(error && error.status || 0) === 404 "
            "&& settleMissingRunRoute(runId)",
            load,
        )
        self.assertIn("route.runId !== runId", settle)
        self.assertIn("state.selectedRunId = null;", settle)
        self.assertIn("state.selectedRun = null;", settle)
        self.assertIn("syncStudioRoute();", settle)

    def test_opening_an_exact_run_reveals_it_in_the_filtered_list(self) -> None:
        reveal = _function_source(self.source, "revealRunInList")
        load = _function_source(self.source, "loadRunDetail")

        self.assertIn(
            "!runMatchesStatusFilter(runStatus(run), state.runStatusFilter)",
            reveal,
        )
        self.assertIn('state.runStatusFilter = "all";', reveal)
        self.assertIn("query && !runSearchText(run).includes(query)", reveal)
        self.assertIn('els.runFilter.value = "";', reveal)
        self.assertLess(
            load.index("revealRunInList(runId);"),
            load.index("state.selectedRunId = runId;"),
        )


if __name__ == "__main__":
    unittest.main()
