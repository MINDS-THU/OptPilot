"""Focused browser-client contracts for Run Workbench actions."""

from __future__ import annotations

import unittest
from pathlib import Path


_APP_JS = (
    Path(__file__).resolve().parents[2]
    / "studio"
    / "src"
    / "optpilot_studio"
    / "ui"
    / "static"
    / "app.js"
)
_STYLES_CSS = _APP_JS.with_name("styles.css")
_INDEX_HTML = _APP_JS.with_name("index.html")


def _function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"function {name}(")
    end = source.index(f"function {next_name}(", start)
    return source[start:end]


def _async_function_source(source: str, name: str, next_name: str) -> str:
    start = source.index(f"async function {name}(")
    end = source.index(f"async function {next_name}(", start)
    return source[start:end]


class StudioWorkbenchStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _APP_JS.read_text(encoding="utf-8")
        cls.styles = _STYLES_CSS.read_text(encoding="utf-8")
        cls.html = _INDEX_HTML.read_text(encoding="utf-8")

    def test_keep_action_is_capability_driven_workspace_derivation(self) -> None:
        label = _function_source(
            self.source,
            "capabilityActionLabel",
            "capabilityReason",
        )
        actions = _function_source(
            self.source,
            "renderSelectionActions",
            "renderSelectionActionControl",
        )
        control = _function_source(
            self.source,
            "renderSelectionActionControl",
            "selectionActionDescription",
        )
        descriptions = _function_source(
            self.source,
            "selectionActionDescription",
            "workbenchActionKey",
        )
        capability = _function_source(
            self.source,
            "actionCapability",
            "renderSelectionActions",
        )
        durable_key = _function_source(
            self.source,
            "durableWorkbenchIntentKey",
            "durableWorkbenchRequestId",
        )

        self.assertIn(
            'workspaceId ? "Open Workspace" : "Edit in Workspace"', label
        )
        self.assertIn(
            'if (action === "open_read_only") return "View files";', label
        )
        self.assertIn('"open_read_only", "keep_editable"', actions)
        self.assertIn(".filter((action) => action.supported)", actions)
        self.assertIn('action.workspace_id ? "Opening…" : "Creating…"', control)
        self.assertIn("workspace_id:", capability)
        self.assertIn("workspace_title:", capability)
        self.assertIn(
            "Create an editable Workspace for this Candidate.",
            descriptions,
        )
        self.assertIn(
            "Open the editable Workspace already linked to this Candidate.",
            descriptions,
        )
        self.assertIn('actionName !== "evaluate_child_run"', durable_key)
        self.assertNotIn("keep_editable", durable_key)

    def test_shortlist_polling_preserves_focused_and_dirty_edits(self) -> None:
        refresh = _function_source(
            self.source,
            "shouldRefreshSelectedRunDetail",
            "shortlistEditingInProgress",
        )
        editing = _function_source(
            self.source,
            "shortlistEditingInProgress",
            "runSummaryChanged",
        )

        self.assertIn("if (shortlistEditingInProgress()) return false;", refresh)
        self.assertIn('state.activeRunTab !== "review"', editing)
        self.assertIn("draft && draft.dirty", editing)
        self.assertIn("document.activeElement", editing)
        self.assertIn('[data-review-title], [data-review-note]', editing)

    def test_study_launch_stays_on_study_until_canonical_run_handoff(self) -> None:
        launch = _async_function_source(
            self.source,
            "launchPlan",
            "submitStudyLaunch",
        )
        handoff = _async_function_source(
            self.source,
            "handoffStudyLaunchIfReady",
            "stopActiveStudyLaunch",
        )
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyLaunchForPlan",
        )

        self.assertIn('schema: "optpilot.studio-study-launch-request.v1"', launch)
        self.assertIn("request_id: requestId", launch)
        self.assertIn("persistActiveStudyLaunch(active)", launch)
        self.assertNotIn('setView("runs")', launch)
        self.assertIn("active.launch.run_id", handoff)
        self.assertIn('state.view === "experiments"', handoff)
        self.assertIn("state.selectedPlanId === active.planId", handoff)
        self.assertIn("if (shouldOpenRun) {\n    state.selectedRunId = runId;", handoff)
        self.assertIn("if (!shouldOpenRun) return true;", handoff)
        self.assertIn('setView("runs")', handoff)
        self.assertIn("!launchPreparing", detail)
        self.assertIn("renderStudyLaunchStatus(plan)", detail)

    def test_study_launch_supports_non_blocking_preparation_responses(self) -> None:
        request_id = _function_source(
            self.source,
            "studyLaunchPreparationRequestId",
            "studyLaunchPreparationState",
        )
        preparation = _function_source(
            self.source,
            "applyStudyLaunchPreparationPayload",
            "submitStudyLaunch",
        )
        submit = _async_function_source(
            self.source,
            "submitStudyLaunch",
            "pollStudyLaunchRequest",
        )
        poll = _async_function_source(
            self.source,
            "pollStudyLaunchRequest",
            "pollStudyLaunch",
        )
        resume = _function_source(
            self.source,
            "resumeStoredStudyLaunch",
            "studyLaunchPreparationRequestId",
        )
        persist = _function_source(
            self.source,
            "persistActiveStudyLaunch",
            "resumeStoredStudyLaunch",
        )

        self.assertIn("payload.request_id || payload.requestId", request_id)
        self.assertIn('return "launch";', preparation)
        self.assertIn('["preparing", "pending", "accepted"]', preparation)
        self.assertIn('preparationState === "uncertain"', preparation)
        self.assertIn('outcome === "preparing"', submit)
        self.assertIn("await pollStudyLaunchRequest(active, generation)", submit)
        self.assertIn("/api/studies/launch-requests/", poll)
        self.assertIn('outcome === "uncertain"', poll)
        self.assertIn("await sleep(1200)", poll)
        self.assertIn("await submitStudyLaunch(active, generation)", poll)
        self.assertIn("active.preparationAccepted", resume)
        self.assertIn("pollStudyLaunchRequest(active, generation", resume)
        self.assertIn("failure: active.failure || null", persist)
        self.assertIn("failure: stored.failure || null", resume)
        self.assertIn("if (studyLaunchIsTerminal(active)) return;", resume)
        self.assertIn("persistActiveStudyLaunch(active)", submit)
        self.assertIn("persistActiveStudyLaunch(active)", poll)

    def test_study_launch_preparation_has_honest_progress_and_single_ownership(self) -> None:
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyLaunchForPlan",
        )
        status = _function_source(
            self.source,
            "renderStudyLaunchStatus",
            "studyConfigEditor",
        )
        elapsed = _function_source(
            self.source,
            "studyLaunchElapsedSeconds",
            "studyLaunchElapsedText",
        )
        launch = _async_function_source(
            self.source,
            "launchPlan",
            "submitStudyLaunch",
        )

        self.assertIn("otherLaunchPreparing", detail)
        self.assertIn("trackedLaunch.planId !== plan.id", detail)
        self.assertIn("trackedLaunch.planId !== plan.id", launch)
        self.assertIn("Another Run is being prepared", launch)
        self.assertIn('stage: "Preparing Run"', launch)
        self.assertNotIn('stage: "Sending launch request"', launch)
        self.assertIn("Date.now() - startedAt", elapsed)
        self.assertIn("studyLaunchElapsedText(active)", status)
        self.assertIn('launch.can_stop && !active.stopPending', status)
        self.assertIn('active.status || "preparing"', status)

    def test_method_exchange_timeout_is_owned_by_method_not_study_ui(self) -> None:
        editor = _function_source(
            self.source,
            "studyConfigEditor",
            "studyAdvancedGroup",
        )
        launch = _async_function_source(
            self.source,
            "launchPlan",
            "submitStudyLaunch",
        )
        payload = _function_source(
            self.source,
            "planPayload",
            "exactCatalogEntryRef",
        )
        yaml_preview = _function_source(
            self.source,
            "planYamlPreview",
            "exactCatalogBindingPreview",
        )
        update = _function_source(
            self.source,
            "updatePlanField",
            "convertSavedPlanToDraft",
        )
        saved_draft_plan = _function_source(
            self.source,
            "savedStudyDraftPlan",
            "buildPlans",
        )
        catalog_plans = _function_source(
            self.source,
            "buildPlans",
            "renderWorkspace",
        )
        new_plan = _function_source(
            self.source,
            "planFromPair",
            "planYamlPreview",
        )

        self.assertNotIn('"Method callback timeout"', editor)
        self.assertNotIn("method_request_timeout_seconds", launch)
        self.assertNotIn("methodRequestTimeoutSeconds", payload)
        self.assertNotIn("methodRequestTimeoutSeconds", yaml_preview)
        for plan_source in (saved_draft_plan, catalog_plans, new_plan):
            self.assertNotIn("methodRequestTimeoutSeconds", plan_source)
        self.assertNotIn("methodRequestTimeoutSeconds", update)

    def test_pre_handoff_launches_are_not_public_run_rows(self) -> None:
        loading = _async_function_source(
            self.source,
            "loadRunsAndJobs",
            "loadRunDetail",
        )
        render = _function_source(
            self.source,
            "renderRuns",
            "runMatchesStatusFilter",
        )

        self.assertIn(
            'getJson("/api/runs", { timeoutMs: RUNS_REQUEST_TIMEOUT_MS })',
            loading,
        )
        self.assertNotIn("/api/jobs", loading)
        self.assertIn("head: run.head ?? catalogEntry.head", loading)
        self.assertIn("const rows = state.runs;", render)
        self.assertNotIn("runRowsWithJobs", self.source)
        self.assertNotIn('startsWith("job:")', self.source)

    def test_run_rows_surface_comparable_candidate_best_and_update_time(self) -> None:
        planned = _function_source(
            self.source,
            "runPlannedWork",
            "runBestPrimaryValue",
        )
        best = _function_source(
            self.source,
            "runBestPrimaryValue",
            "renderRunStopConfirmation",
        )
        row = _function_source(
            self.source,
            "runRow",
            "tableFromRows",
        )

        self.assertIn('"max_trials"', planned)
        self.assertIn("finished · no trial limit", planned)
        self.assertIn("terminalTrials", planned)
        self.assertIn("Planned work unavailable", planned)
        self.assertIn("best_comparable_candidate", best)
        self.assertNotIn("run.best_metric", best)
        self.assertIn("runPlannedWork(run)", row)
        self.assertIn("runBestPrimaryValue(run)", row)
        self.assertIn("Trials:", row)
        self.assertIn("Best comparable Candidate ${escapeHtml(best.label)}", row)
        self.assertIn("runOverviewBestReason(best.reason)", row)
        self.assertNotIn("Updated ${updated}", row)

    def test_stop_run_requires_an_explicit_evidence_preserving_confirmation(self) -> None:
        detail = _function_source(
            self.source,
            "renderRunDetail",
            "selectRunActionContext",
        )
        confirmation = _function_source(
            self.source,
            "renderRunStopConfirmation",
            "openRunStopConfirmation",
        )
        submit = _function_source(
            self.source,
            "confirmRunStop",
            "exactRunOverview",
        )

        self.assertIn("openRunStopConfirmation(run, stopRunButton)", detail)
        self.assertNotIn("/cancel", detail)
        self.assertIn("Remains available:", confirmation)
        self.assertIn("other results already recorded in this Run", confirmation)
        self.assertIn("only future Candidate generation and evaluation work", confirmation)
        self.assertIn('schema: "optpilot.run-cancel-request.v1"', submit)
        self.assertIn("request_id: pending.requestId", submit)
        self.assertIn("/cancel", submit)
        self.assertIn("pending.submitting", submit)
        self.assertIn("closeRunStopConfirmation({ restoreFocus: false })", submit)

    def test_study_launch_recovery_and_stop_use_durable_browser_ids(self) -> None:
        resume = _function_source(
            self.source,
            "resumeStoredStudyLaunch",
            "submitStudyLaunch",
        )
        stop_start = self.source.index("async function stopActiveStudyLaunch(")
        stop_end = self.source.index("function isEmbeddedCodeWorkspaceActive(", stop_start)
        stop = self.source[stop_start:stop_end]

        self.assertIn("stored.requestId", resume)
        self.assertIn("submitStudyLaunch(active, generation)", resume)
        self.assertIn("pollStudyLaunchRequest(active, generation", resume)
        self.assertIn("active.stopRequestId = active.stopRequestId || newRequestId()", stop)
        self.assertIn('schema: "optpilot.studio-study-launch-stop-request.v1"', stop)
        self.assertIn("request_id: active.stopRequestId", stop)

    def test_unavailable_saved_study_stays_visible_but_cannot_launch(self) -> None:
        saved = _function_source(
            self.source,
            "savedStudyDraftPlan",
            "buildPlans",
        )
        detail = _function_source(
            self.source,
            "renderPlanDetail",
            "studyConfigEditor",
        )
        source_note = _function_source(
            self.source,
            "studySourceNote",
            "hasWorkspaceStudyDraft",
        )

        self.assertIn("availability.available !== false", saved)
        self.assertIn('? "unavailable"', saved)
        self.assertIn("unavailableReason: availability.reason", saved)
        self.assertIn("const draftUnavailable", detail)
        self.assertIn("&& !draftUnavailable", detail)
        self.assertIn("const locked = draftUnavailable", detail)
        self.assertIn("plan.draft.available === false", source_note)
        self.assertIn("remains listed so you can review the problem", source_note)
        self.assertIn(".source-note-error", self.styles)

    def test_run_list_keeps_valid_rows_and_explains_unavailable_saved_runs(
        self,
    ) -> None:
        loading_start = self.source.index("async function loadRunsAndJobs(")
        loading_end = self.source.index(
            "function shouldRefreshSelectedRunDetail(", loading_start
        )
        loading = self.source[loading_start:loading_end]
        notice = _function_source(
            self.source,
            "runProjectionNotice",
            "renderRuns",
        )
        rendering = _function_source(
            self.source,
            "renderRuns",
            "runMatchesStatusFilter",
        )

        self.assertIn("runsPayload.unavailable", loading)
        self.assertIn("older", notice)
        self.assertIn("not be shown", notice)
        self.assertIn("Existing Run data was not changed", notice)
        self.assertIn('role="status"', notice)
        self.assertIn("runProjectionNotice(state.runUnavailable)", rendering)
        self.assertIn(".run-projection-notice", self.styles)

    def test_failed_run_guidance_prefers_attempt_evidence(self) -> None:
        loading = _async_function_source(
            self.source,
            "loadRunDetail",
            "loadSelectedRunOperatorJobs",
        )
        target = _function_source(
            self.source,
            "failedRunEvidenceTarget",
            "initialRunDetailTab",
        )
        initial_tab = _function_source(
            self.source,
            "initialRunDetailTab",
            "runProgressGuidance",
        )
        guidance = _function_source(
            self.source,
            "runProgressGuidance",
            "renderRunDetail",
        )

        self.assertIn('workbenchPage(detail, "attempt").items.length', target)
        self.assertIn(
            'return { tab: "attempt", label: "Review trial attempts" }',
            target,
        )
        self.assertIn(
            "state.activeRunTab = initialRunDetailTab(detail)",
            loading,
        )
        self.assertIn('runStatus(summary) !== "failed"', initial_tab)
        self.assertIn("failedRunEvidenceTarget(detail, runCounts(summary)).tab", initial_tab)
        self.assertIn("failedRunEvidenceTarget(detail, counts)", guidance)
        self.assertIn('data-run-tab="${escapeHtml(evidence.tab)}"', guidance)
        self.assertIn('data-run-tab="timeline"', guidance)

    def test_run_list_poll_failure_marks_selected_detail_as_stale(self) -> None:
        loading_start = self.source.index("async function loadRunsAndJobs(")
        loading_end = self.source.index(
            "function shouldRefreshSelectedRunDetail(", loading_start
        )
        loading = self.source[loading_start:loading_end]
        detail = _function_source(
            self.source,
            "renderRunDetail",
            "selectRunActionContext",
        )
        refresh = _function_source(
            self.source,
            "runDetailRefreshNoticeHtml",
            "bindRunDetailRefreshButton",
        )
        in_place = _function_source(
            self.source,
            "updateRunDetailRefreshNoticeInPlace",
            "renderRunDetail",
        )

        self.assertIn('if (state.view === "runs") {', loading)
        self.assertIn(
            "if (shortlistEditingInProgress()) updateRunDetailRefreshNoticeInPlace();",
            loading,
        )
        self.assertIn("else renderRunDetail();", loading)
        self.assertIn("const recoveredFromRunsError", loading)
        self.assertIn(
            "if (recoveredFromRunsError && !refreshDetail)",
            loading,
        )
        self.assertIn("const refreshErrorSource = state.runsError", refresh)
        self.assertIn('? "detail"', refresh)
        self.assertIn('? "list"', refresh)
        self.assertIn(
            'data-refresh-run-detail="${escapeHtml(refreshErrorSource)}"',
            refresh,
        )
        self.assertIn("updateRunDetailRefreshNoticeInPlace()", loading)
        self.assertIn('insertAdjacentHTML("afterend", noticeHtml)', in_place)
        self.assertIn("runDetailRefreshNoticeHtml(run, summary)", detail)

    def test_stale_run_detail_distinguishes_recorded_update_from_refresh_time(
        self,
    ) -> None:
        notice = _function_source(
            self.source,
            "runDetailRefreshNoticeHtml",
            "bindRunDetailRefreshButton",
        )

        self.assertIn("const recordedUpdateValue", notice)
        self.assertIn("const lastRecordedRunUpdate", notice)
        self.assertIn("const lastSuccessfulRefresh", notice)
        self.assertIn("Last successful refresh:", notice)
        self.assertIn("Last recorded Run update:", notice)
        self.assertNotIn("Showing the last loaded Run details", notice)

    def test_zero_active_trial_guidance_waits_then_offers_recovery_actions(
        self,
    ) -> None:
        scheduling = _function_source(
            self.source,
            "scheduleRunHandoffGuidance",
            "failedRunEvidenceTarget",
        )
        guidance = _function_source(
            self.source,
            "runProgressGuidance",
            "renderRunDetail",
        )

        self.assertIn("RUN_ZERO_ACTIVE_GUIDANCE_DELAY_MS = 12_000", self.source)
        self.assertIn("window.setTimeout", scheduling)
        self.assertIn("state.selectedRunId === runId", scheduling)
        self.assertIn(
            "idleMilliseconds < RUN_ZERO_ACTIVE_GUIDANCE_DELAY_MS", guidance
        )
        self.assertIn("scheduleRunHandoffGuidance", guidance)
        self.assertIn('data-refresh-run-detail="detail"', guidance)
        self.assertIn('data-run-tab="timeline"', guidance)
        self.assertIn("Waiting for the next trial", guidance)
        self.assertIn(".run-progress-guidance > .action-row", self.styles)

    def test_shortlist_is_one_atomic_run_local_decision_workflow(self) -> None:
        tabs = _function_source(self.source, "runWorkbenchTabs", "workbenchPage")
        candidate = _function_source(
            self.source,
            "renderCandidateReviewAction",
            "renderCandidateComparisonPanel",
        )
        review = _function_source(
            self.source,
            "renderReviewCollection",
            "renderReviewItem",
        )
        inspection = _function_source(
            self.source,
            "renderReviewInspectionOutcomes",
            "loadedObservationItems",
        )
        job_action = _function_source(
            self.source,
            "renderOperatorJobReviewAction",
            "renderOperatorJobInterfaceAction",
        )
        payload = _function_source(
            self.source,
            "reviewCollection",
            "reviewContainsCandidate",
        )
        command_draft = _function_source(
            self.source,
            "shortlistCommandDraft",
            "shortlistCommandParameters",
        )
        mutation_request = _function_source(
            self.source,
            "shortlistMutationRequest",
            "completeShortlistMutationIntent",
        )
        add = _async_function_source(
            self.source,
            "addCandidateToReview",
            "saveReviewDraft",
        )
        save = _async_function_source(
            self.source,
            "saveReviewDraft",
            "deleteReviewCollection",
        )
        delete = _async_function_source(
            self.source,
            "deleteReviewCollection",
            "attachOperatorJobToReview",
        )
        attach = _async_function_source(
            self.source,
            "attachOperatorJobToReview",
            "exportReviewRevision",
        )
        export = self.source[
            self.source.index("async function exportReviewRevision(") : self.source.index(
                "function entityStateTags("
            )
        ]

        self.assertIn('["review", "Shortlist"]', tabs)
        self.assertIn("if (shortlist) {", tabs)
        self.assertNotIn("shortlist.items.length", tabs)
        self.assertIn("Save to Shortlist", candidate)
        self.assertIn("does not create a Workspace or run the Candidate", candidate)
        self.assertIn('role="alert"', candidate)
        self.assertIn("Try again", candidate)
        self.assertIn("state.reviewSelectionErrors[selectionId]", candidate)
        self.assertNotIn("state.reviewError", candidate)
        self.assertIn("Keep promising Candidates", review)
        self.assertIn("const error = state.reviewError", review)
        self.assertIn("Save changes", review)
        self.assertIn("Version history", review)
        self.assertIn("Load older history", review)
        self.assertIn("Return to current", review)
        self.assertIn("Delete Shortlist", review)
        self.assertIn('class="review-more"', review)
        self.assertIn("readonly", review)
        # The renderer consumes the raw Shortlist payload; the legacy
        # review_collection bridge field must not come back.
        self.assertIn("detail.shortlist", payload)
        self.assertIn("collection.cards", review)
        self.assertNotIn(".review_collection =", self.source)
        self.assertIn("shortlist_id: draft.shortlist_id", command_draft)
        self.assertIn("expected_revision: draft.expected_revision", command_draft)
        self.assertIn("cards: draft.items.map", command_draft)
        self.assertIn('optpilot.run-shortlist-command.v1', mutation_request)
        self.assertIn("fingerprint", mutation_request)
        self.assertIn("durableShortlistIntents", mutation_request)
        self.assertIn("Update saved result", candidate)
        self.assertIn("savedSelectionDigest !== currentSelectionDigest", candidate)
        self.assertIn("Saved result:", candidate)
        self.assertIn("matches the current exact result", candidate)
        self.assertIn('data-update-saved-result="true"', candidate)
        self.assertIn('"save_candidate"', add)
        self.assertIn("shortlistCommandDraft()", add)
        self.assertIn("operator_job_id: operatorJobId || null", add)
        self.assertIn("update_saved_result: updateSavedResult", add)
        self.assertIn("source Run is no longer open", add)
        self.assertIn("could not be resolved from the current Run", add)
        self.assertIn("state.reviewSelectionErrors[selectionId]", add)
        self.assertNotIn("state.reviewError", add)
        self.assertIn('"save_changes"', save)
        self.assertIn("shortlistCommandDraft()", save)
        self.assertIn('command: "delete"', delete)
        self.assertIn("window.confirm", delete)
        self.assertIn("all ${revisionCount} saved version", delete)
        self.assertIn("expected_revision_digest: collection.revision_digest", delete)
        self.assertIn('confirmation: "delete_review_collection"', delete)
        self.assertIn("optpilot.review-collection-deletion.v1", delete)
        self.assertIn('"attach_inspection"', attach)
        self.assertIn("shortlistCommandDraft()", attach)
        self.assertIn("state.reviewOperatorJobErrors[jobId]", attach)
        self.assertNotIn("state.reviewError", attach)
        self.assertIn("preserving your pending Shortlist notes and order", job_action)
        self.assertIn("Save Candidate and try result", job_action)
        self.assertIn("finished result together in the Shortlist", job_action)
        self.assertIn('!["succeeded", "failed"].includes(job.state)', job_action)
        self.assertIn("!job || !result", job_action)
        self.assertIn("state.reviewOperatorJobErrors[job.job_id]", job_action)
        self.assertIn("Saved try results", inspection)
        self.assertIn("optpilot.review-inspection-outcome.v1", inspection)
        self.assertIn("shortlistCommandParameters()", save)
        self.assertIn("format=export", export)
        self.assertIn("exported.revision_digest !== collection.revision_digest", export)
        self.assertIn("async function openReviewRevision", export)
        self.assertIn("async function loadOlderReviewHistory", export)
        self.assertIn("before_revision=", export)
        self.assertNotIn("localStorage", add + save + delete + attach + export)
        for selector in (
            ".review-collection",
            ".review-shortlist",
            ".review-history-bar",
            ".review-item",
            ".review-note-field",
            ".review-inspection-evidence",
            ".operator-job-review-action",
        ):
            self.assertIn(selector, self.styles)

    def test_candidate_capability_reasons_use_plain_user_language(self) -> None:
        reasons = _function_source(
            self.source,
            "capabilityReason",
            "renderWorkbenchPage",
        )

        self.assertIn(
            'debug_run_provider_mismatch: "The software setup saved with this Run is not available now.',
            reasons,
        )
        self.assertIn(
            "environment_preview_profile_unavailable: \"This Run's saved Environment version does not include an interactive interface.",
            reasons,
        )
        self.assertIn(
            'operator_job_provider_unavailable: "This OptPilot installation cannot try Candidates.',
            reasons,
        )
        self.assertIn(
            "environment_preview_profile_incompatible: \"This Environment's interactive interface cannot run in the current OptPilot installation.",
            reasons,
        )
        self.assertIn('return messages[code] || "This action is unavailable for this Candidate."', reasons)

    def test_keep_response_transitions_to_workspace_without_operator_job_refresh(
        self,
    ) -> None:
        action = _function_source(
            self.source,
            "performWorkbenchAction",
            "renderCandidateInspection",
        )
        keep_start = action.index('} else if (actionName === "keep_editable") {')
        operator_job_start = action.index(
            "const job = operatorJobFromPayload(payload);"
        )
        keep_branch = action[keep_start:operator_job_start]

        self.assertLess(keep_start, operator_job_start)
        self.assertIn(
            "const workspace = mergeUiWorkspace(payload.workspace);", keep_branch
        )
        self.assertIn("keepWorkspaceSelected(workspace.id);", keep_branch)
        self.assertNotIn("attachWorkspaceToCurrent", keep_branch)
        self.assertIn('setView("workspace");', keep_branch)
        self.assertIn(
            'if (!["inspect", "open_read_only", "keep_editable", "evaluate_child_run"].includes(actionName)) loadSelectedRunOperatorJobs({ silent: true });',
            action,
        )

    def test_re_evaluate_uses_a_complete_read_only_confirmation_plan(self) -> None:
        actions = _function_source(
            self.source,
            "renderSelectionActions",
            "renderSelectionActionControl",
        )
        open_confirmation = _function_source(
            self.source,
            "openChildRunConfirmation",
            "closeChildRunConfirmation",
        )
        confirmation = _function_source(
            self.source,
            "renderChildRunConfirmation",
            "shortDigest",
        )
        action = _function_source(
            self.source,
            "performWorkbenchAction",
            "renderCandidateInspection",
        )

        self.assertIn('"evaluate_child_run"', actions)
        self.assertIn('preset.id !== "re_evaluate_exact_plan"', open_confirmation)
        self.assertIn(
            'preset.schema !== "optpilot.re-evaluate-exact-plan-preset.v1"',
            open_confirmation,
        )
        self.assertIn("coordinates.length !== logicalTrials", open_confirmation)
        self.assertIn("logicalTrials > 100", open_confirmation)
        self.assertIn("maxTrials !== logicalTrials", open_confirmation)
        self.assertIn("preset.method_proposals !== false", open_confirmation)
        self.assertIn("preset.environment.revision", open_confirmation)
        self.assertIn("Evaluate Candidate ${pending.candidate_id} again?", confirmation)
        self.assertIn("Environment version", confirmation)
        self.assertIn("Trial settings", confirmation)
        self.assertIn("Start re-evaluation", confirmation)
        candidate_details = _function_source(
            self.source,
            "renderFocusedCandidateMore",
            "ensureFocusedCandidateInspection",
        )
        self.assertIn("<dt>Environment</dt>", candidate_details)
        self.assertIn("<dt>Environment version</dt>", candidate_details)
        self.assertIn("Seeds and repetitions", confirmation)
        self.assertIn("exact Candidate, Environment version, seeds, repetitions", confirmation)
        self.assertIn("source Run is unchanged", confirmation)
        self.assertNotIn("<input", confirmation)
        self.assertNotIn("<select", confirmation)
        self.assertIn(
            'schema: "optpilot.re-evaluate-exact-plan-confirmation.v1"',
            action,
        )
        self.assertIn(
            "expected_parent_seal_digest: confirmedChildPlan.preset.parent_seal_digest",
            action,
        )
        self.assertIn(
            "expected_plan_digest: confirmedChildPlan.preset.plan_digest",
            action,
        )
        self.assertIn("await loadRunDetail(childRunId);", action)
        self.assertNotIn(
            "operatorJobFromPayload(payload)",
            action[
                action.index(
                    '} else if (actionName === "evaluate_child_run") {'
                ) : action.index(
                    "} else {",
                    action.index('} else if (actionName === "evaluate_child_run") {'),
                )
            ],
        )

    def test_selection_coordinates_are_disclosed_only_as_technical_details(
        self,
    ) -> None:
        technical = _function_source(
            self.source,
            "renderSelectionTechnicalDetails",
            "renderSpecializedWorkbenchBody",
        )
        specialized = _function_source(
            self.source,
            "renderSpecializedWorkbenchBody",
            "renderWorkbenchItem",
        )
        generic = _function_source(
            self.source,
            "renderWorkbenchItem",
            "actionCapability",
        )

        self.assertIn("<summary>Technical details</summary>", technical)
        self.assertIn("<dt>Run revision</dt>", technical)
        self.assertIn("<dt>Sequence</dt>", technical)
        self.assertIn("renderSelectionTechnicalDetails(selection, item)", specialized)
        self.assertIn("renderSelectionTechnicalDetails(selection, item)", generic)
        self.assertNotIn('<p class="workbench-selection-coordinate">', specialized)
        self.assertNotIn('<p class="workbench-selection-coordinate">', generic)

    def test_run_lineage_links_source_candidate_and_re_evaluation_runs(self) -> None:
        lineage = _function_source(
            self.source,
            "runLineageHtml",
            "renderRunDetail",
        )
        detail = _function_source(
            self.source,
            "renderRunDetail",
            "selectRunActionContext",
        )

        self.assertIn('lineage.schema !== "optpilot.run-lineage-summary.v1"', lineage)
        self.assertIn("Re-evaluation of Candidate", lineage)
        self.assertIn("Launched from Run setup", lineage)
        self.assertIn('data-open-lineage-run=', lineage)
        self.assertIn('data-open-lineage-candidate=', lineage)
        self.assertIn("runLineageHtml(detail.lineage, run.name)", detail)
        self.assertIn('[data-open-lineage-run]', detail)
        self.assertIn("state.routedCandidateId = candidateId || null", detail)

    def test_open_read_only_is_generic_and_does_not_create_a_workspace(self) -> None:
        item = _function_source(
            self.source,
            "renderWorkbenchItem",
            "actionCapability",
        )
        actions = _function_source(
            self.source,
            "renderSelectionActions",
            "renderSelectionActionControl",
        )
        action = _function_source(
            self.source,
            "performWorkbenchAction",
            "renderCandidateInspection",
        )
        open_start = action.index('} else if (actionName === "open_read_only") {')
        keep_start = action.index('} else if (actionName === "keep_editable") {')
        open_branch = action[open_start:keep_start]

        self.assertIn("${renderSelectionActions(item, page)}", item)
        self.assertNotIn('item.kind === "candidate"', item)
        self.assertIn('"inspect", "open_read_only", "keep_editable"', actions)
        self.assertIn("const item = currentWorkbenchItem(selectionId);", action)
        self.assertIn("workbenchPage(state.selectedRun, item.kind)", action)
        self.assertIn(
            "content_session_id: state.selectionContentSessionId || null", action
        )
        self.assertIn(
            "await openSelectionContentView(payload, item, runId);", open_branch
        )
        self.assertNotIn("mergeUiWorkspace", open_branch)
        self.assertNotIn("setView", open_branch)
        self.assertNotIn("attachWorkspace", open_branch)

    def test_content_authority_is_ephemeral_closed_and_never_rendered(self) -> None:
        opened = _async_function_source(
            self.source,
            "openSelectionContentView",
            "loadSelectionContentTree",
        )
        close = _function_source(
            self.source,
            "closeSelectionContentView",
            "releaseSelectionContentViewOnUnload",
        )
        unload = _function_source(
            self.source,
            "releaseSelectionContentViewOnUnload",
            "renderSelectionContentDrawer",
        )
        drawer = _function_source(
            self.source,
            "renderSelectionContentDrawer",
            "renderSelectionContentTree",
        )

        self.assertIn('raw.schema !== "optpilot.selection-content-view.v1"', opened)
        self.assertIn("state.selectionContentSessionId = contentSessionId;", opened)
        self.assertNotIn("storeValue", opened)
        self.assertNotIn("localStorage", opened)
        self.assertLess(
            close.index("state.selectionContentRequestSeq += 1;"),
            close.index("state.selectionContentView = null;"),
        )
        self.assertIn(
            'schema: "optpilot.selection-content-view-close-request.v1"', close
        )
        self.assertIn("content_session_id: contentSessionId", close)
        self.assertIn("keepalive: true", unload)
        self.assertNotIn("view.handle", drawer)
        self.assertNotIn("contentSessionId", drawer)

    def test_content_tree_and_preview_are_relative_paginated_and_bounded(self) -> None:
        tree = _function_source(
            self.source,
            "loadSelectionContentTree",
            "normalizeSelectionContentTreePage",
        )
        preview = _function_source(
            self.source,
            "loadSelectionContentPreview",
            "normalizeSelectionContentPreview",
        )
        normalize = _function_source(
            self.source,
            "normalizeSelectionContentPreview",
            "safeSelectionRelativePath",
        )
        safe_path = _function_source(
            self.source,
            "safeSelectionRelativePath",
            "currentSelectionContentRequest",
        )
        rendered = _function_source(
            self.source,
            "renderSelectionContentPreview",
            "selectionContentBreadcrumb",
        )
        content_slice = self.source[
            self.source.index(
                "async function openSelectionContentView("
            ) : self.source.index("function bindWorkbenchEntityActions(")
        ]

        self.assertIn("/tree?${params.toString()}", tree)
        self.assertIn('params.set("page_token", pageToken)', tree)
        self.assertIn("SELECTION_CONTENT_TREE_ENTRY_LIMIT", tree)
        self.assertIn("/content?${params.toString()}", preview)
        self.assertIn('params.set("relative_path", selectedPath)', preview)
        self.assertIn("SELECTION_CONTENT_PREVIEW_LIMIT", preview)
        self.assertIn("escapeHtml(textContent)", rendered)
        self.assertIn("Binary content cannot be previewed here", rendered)
        self.assertIn('value: encoding === "utf-8" ? value : ""', normalize)
        self.assertIn('path.includes("\\\\")', safe_path)
        self.assertIn('part === ".."', safe_path)
        self.assertIn("payload.encoding", normalize)
        self.assertIn("optpilot.selection-content-byte-range.v1", normalize)
        self.assertNotIn("content_ref", content_slice)
        self.assertNotIn("host_path", content_slice)
        self.assertIn("Saved result file list", content_slice)
        self.assertIn("This saved folder is empty.", content_slice)
        self.assertIn("Read-only file preview", content_slice)
        self.assertNotIn("Retained content tree", content_slice)
        self.assertNotIn("This retained tree", content_slice)
        self.assertNotIn("Tree preview limit", content_slice)

    def test_content_drawer_is_compact_scrollable_and_responsive(self) -> None:
        for selector in (
            ".selection-content-drawer",
            ".selection-content-layout",
            ".selection-content-tree-list",
            ".selection-content-breadcrumb",
            ".selection-content-preview-body",
        ):
            self.assertIn(selector, self.styles)
        self.assertIn("position: fixed;", self.styles)
        self.assertIn("overflow: auto;", self.styles)
        self.assertIn("width: min(1040px, calc(100vw - 320px));", self.styles)
        self.assertIn("@media (max-width: 1050px)", self.styles)
        self.assertIn("@media (max-width: 820px)", self.styles)

    def test_content_drawer_uses_plain_hierarchy_and_keyboard_close(self) -> None:
        rendered = _function_source(
            self.source,
            "renderSelectionContentDrawer",
            "renderSelectionContentTree",
        )
        tree = _function_source(
            self.source,
            "renderSelectionContentTree",
            "renderSelectionContentPreview",
        )
        keyboard = _function_source(
            self.source,
            "handleSelectionContentKeydown",
            "renderSelectionContentDrawer",
        )

        self.assertIn("Read-only result", rendered)
        self.assertIn('<h2 id="selection-content-title"', rendered)
        self.assertIn('aria-labelledby="selection-content-title"', rendered)
        self.assertNotIn("Open · read-only", rendered)
        self.assertIn("Select a file to preview", tree)
        self.assertIn("${escapeHtml(entries.length)} shown", tree)
        self.assertIn('aria-current="true"', tree)
        self.assertNotIn('aria-hidden="true">▸', tree)
        self.assertIn('event.key !== "Escape"', keyboard)
        self.assertIn("closeSelectionContentView()", keyboard)
        self.assertIn('"content"', self.styles)
        self.assertIn("grid-area: content;", self.styles)
        self.assertIn("white-space: pre-wrap;", self.styles)

    def test_environment_preview_uses_retained_profile_and_job_presentation(
        self,
    ) -> None:
        capability = _function_source(
            self.source,
            "actionCapability",
            "renderSelectionActions",
        )
        control = _function_source(
            self.source,
            "renderSelectionActionControl",
            "selectionActionDescription",
        )
        action = _function_source(
            self.source,
            "performWorkbenchAction",
            "renderCandidateInspection",
        )
        presentation = _function_source(
            self.source,
            "renderOperatorJobInterfaceAction",
            "renderOperatorJobInterfaceOutputs",
        )

        self.assertIn("selected_profile_id", capability)
        self.assertIn("profile_diagnostics", capability)
        self.assertIn("eligibility_detail", capability)
        self.assertIn("data-preview-profile-selection", control)
        self.assertIn("{ profile_id: previewProfileId }", action)
        self.assertIn('job.job_kind !== "environment-preview"', presentation)
        self.assertIn("presentation.open_url", presentation)
        self.assertIn('title="Interactive Candidate interface"', presentation)

    def test_live_candidate_interface_uses_stable_full_page_host_during_polls(
        self,
    ) -> None:
        session = _function_source(
            self.source,
            "renderInterfaceSession",
            "openCandidateInterfaceSession",
        )
        jobs_refresh = _async_function_source(
            self.source,
            "loadSelectedRunOperatorJobs",
            "loadOperatorJobDetail",
        )
        detail_gate = _function_source(
            self.source,
            "operatorJobNeedsDetailRefresh",
            "upsertOperatorJob",
        )
        panel_refresh = _function_source(
            self.source,
            "renderOperatorJobsPanel",
            "operatorJobsPanelBody",
        )
        panel_body = _function_source(
            self.source,
            "operatorJobsPanelBody",
            "renderOperatorJobRow",
        )
        presentation = _function_source(
            self.source,
            "renderOperatorJobInterfaceAction",
            "renderOperatorJobInterfaceOutputs",
        )

        self.assertIn("els.interfaceSessionFrame", session)
        self.assertIn('getAttribute("src")', session)
        self.assertIn("currentUrl !== model.openUrl", session)
        self.assertIn('setAttribute("src", model.openUrl)', session)
        self.assertNotIn("<iframe", session)
        self.assertIn("showLoadingState", jobs_refresh)
        self.assertIn(
            "operatorJobNeedsDetailRefresh(selectedSummary, state.selectedOperatorJob)",
            jobs_refresh,
        )
        self.assertIn(
            "state.selectedOperatorJob.job_id !== summary.job_id",
            jobs_refresh,
        )
        self.assertIn('summaryPresentation.status === "ready"', detail_gate)
        self.assertIn('detailPresentation.status === "available"', detail_gate)
        self.assertIn("detailPresentation.open_url", detail_gate)
        self.assertIn("return operatorJobIsActive(summary)", detail_gate)
        self.assertIn('state.view === "interface"', panel_refresh)
        self.assertIn("renderInterfaceSession()", panel_refresh)
        self.assertIn("const selected = selectedDetail || selectedSummary", panel_body)
        self.assertNotIn("<iframe", presentation)
        self.assertIn('data-open-operator-interface="${escapeHtml(job.job_id)}"', presentation)

    def test_candidate_tries_hide_internal_operator_job_vocabulary(self) -> None:
        labels = _function_source(
            self.source,
            "capabilityActionLabel",
            "capabilityReason",
        )
        panel = _function_source(
            self.source,
            "operatorJobsPanelBody",
            "renderOperatorJobRow",
        )
        try_label = _function_source(
            self.source,
            "operatorJobLabel",
            "renderOperatorJobSummary",
        )
        summary = _function_source(
            self.source,
            "renderOperatorJobSummary",
            "renderOperatorJobReviewAction",
        )
        presentation = _function_source(
            self.source,
            "renderOperatorJobInterfaceAction",
            "renderOperatorJobInterfaceOutputs",
        )
        outputs = _function_source(
            self.source,
            "renderOperatorJobInterfaceOutputs",
            "renderOperatorJobResult",
        )
        try_outputs = _function_source(
            self.source,
            "operatorJobOutputsForRender",
            "retryOperatorJobOutput",
        )

        self.assertIn('if (action === "debug_run") return "Try once";', labels)
        self.assertIn(
            'if (action === "environment_preview") return "Open interactive interface";',
            labels,
        )
        self.assertIn("<h3>Candidate tries</h3>", panel)
        self.assertIn("not use the Run's trial budget", panel)
        self.assertIn('return "Try once";', try_label)
        self.assertIn('return "Open interactive interface";', try_label)
        self.assertIn("This try does not use the Run's trial budget", summary)
        self.assertIn(
            "OptPilot is finishing cleanup now; if interrupted, it will resume automatically.",
            summary,
        )
        self.assertNotIn("will retry it after restart", summary)
        self.assertIn('<details class="operator-job-more">', summary)
        self.assertIn("<summary>More</summary>", summary)
        self.assertIn('aria-label="Interactive Candidate interface"', presentation)
        self.assertIn('data-open-operator-interface="${escapeHtml(job.job_id)}"', presentation)
        self.assertIn(">Open interface</button>", presentation)
        self.assertNotIn("<iframe", presentation)
        self.assertIn('aria-label="Candidate try outputs"', outputs)
        self.assertIn("kind: output && output.kind", try_outputs)
        for source in (panel, try_label, summary, presentation, outputs):
            for forbidden in (
                "Operator Job",
                "Debug Run",
                "Environment Preview",
                "Realm head",
                "Workbench",
                "Ready trees",
            ):
                self.assertNotIn(forbidden, source)

    def test_candidate_route_renders_one_focused_detail_with_local_try_result(
        self,
    ) -> None:
        page = _function_source(
            self.source,
            "renderCandidateResultsPage",
            "renderFocusedCandidatePage",
        )
        focused = _function_source(
            self.source,
            "renderFocusedCandidatePage",
            "renderFocusedCandidateActions",
        )
        panel = _function_source(
            self.source,
            "operatorJobsPanelBody",
            "renderOperatorJobRow",
        )
        run_detail = _function_source(
            self.source,
            "renderRunDetail",
            "selectRunActionContext",
        )

        self.assertIn("if (state.routedCandidateId)", page)
        self.assertIn(
            "return renderFocusedCandidatePage(detail, page, focusedCandidate);",
            page,
        )
        self.assertIn("Back to Candidates", focused)
        self.assertIn("Results from this Run", focused)
        self.assertIn("Trials with a usable objective", focused)
        self.assertIn(
            "operatorJobsSection(selectedCanonicalRunId(), String(candidate.id",
            focused,
        )
        self.assertIn("allJobs.filter", panel)
        self.assertIn("target.candidate_id", panel)
        self.assertNotIn("operatorJobsSection(runId)", run_detail)

    def test_focused_candidate_has_one_primary_try_action_and_mode_sheet(
        self,
    ) -> None:
        direct_mode = _function_source(
            self.source,
            "directCandidateTryMode",
            "candidateTryPrimaryLabel",
        )
        primary_label = _function_source(
            self.source,
            "candidateTryPrimaryLabel",
            "candidateTrySubmitLabel",
        )
        submit_label = _function_source(
            self.source,
            "candidateTrySubmitLabel",
            "renderFocusedCandidateActions",
        )
        actions = _function_source(
            self.source,
            "renderFocusedCandidateActions",
            "renderFocusedCandidateMore",
        )
        start = _function_source(
            self.source,
            "startCandidateTry",
            "renderCandidateTrySheet",
        )
        sheet = _function_source(
            self.source,
            "renderCandidateTrySheet",
            "updateCandidateTrySheet",
        )
        diagnostics = _function_source(
            self.source,
            "renderCandidatePreviewProfileDiagnostics",
            "renderCandidateTrySheet",
        )
        trust_command = _function_source(
            self.source,
            "shellSingleQuote",
            "renderCandidatePreviewProfileDiagnostics",
        )

        self.assertIn('"Try Candidate"', primary_label)
        self.assertIn("!selectionId || !modes.length || pending", actions)
        self.assertIn("state.workbenchActionErrors", actions)
        self.assertIn('role="alert"', actions)
        self.assertIn("Use Try Candidate to retry.", actions)
        self.assertIn("directCandidateTryMode(modes)", actions)
        self.assertIn("candidateTryPrimaryLabel(modes, pending)", actions)
        self.assertIn("modes.length !== 1", direct_mode)
        self.assertIn('mode.action === "environment_preview"', direct_mode)
        self.assertIn("profiles.length !== 1", direct_mode)
        self.assertIn('return "Try unavailable"', primary_label)
        self.assertIn('return "Why unavailable?"', primary_label)
        self.assertIn("capabilityActionLabel(directMode.action)", primary_label)
        self.assertIn('actionName === "debug_run"', submit_label)
        self.assertIn('actionName === "environment_preview"', submit_label)
        self.assertIn('"debug_run", "environment_preview"', start)
        self.assertIn(".filter((capability) => capability.supported);", start)
        self.assertIn("directCandidateTryMode(modes)", start)
        self.assertNotIn("profiles.length <= 1", start)
        self.assertIn("restoreCandidateTryFocus: true", start)
        self.assertIn("state.pendingCandidateTry", start)
        self.assertIn("Ways to try it", sheet)
        self.assertIn('class="candidate-try-mode unavailable"', sheet)
        self.assertIn("!selectedMode.eligible", sheet)
        self.assertIn("Try once", self.source)
        self.assertIn("Open interactive interface", self.source)
        self.assertIn("candidateTrySubmitLabel(selectedMode.action)", sheet)
        self.assertIn(
            'id="candidateTrySubmitButton" class="primary-button" type="button">Try once</button>',
            self.html,
        )
        self.assertNotIn("Start try", self.source)
        self.assertNotIn(">Start try</button>", self.html)
        self.assertIn("data-candidate-try-profile", sheet)
        self.assertIn(
            "renderCandidateInspectionPlan(selectedMode.inspection_plan",
            sheet,
        )
        self.assertIn("cannot currently be tried", sheet)
        self.assertIn("Why it is unavailable", sheet)
        self.assertIn("profile_diagnostics", diagnostics)
        self.assertIn("profile.applicable !== false", diagnostics)
        self.assertIn("approve_container_gateway_image", trust_command)
        self.assertIn(
            "optpilot environment-preview trust approve", trust_command
        )
        self.assertIn("shellSingleQuote(imageRef)", trust_command)
        self.assertIn("escapeHtml(command)", diagnostics)
        self.assertIn("data-copy-preview-trust-command", diagnostics)
        self.assertIn("then restart Studio", diagnostics)
        self.assertIn("exact session-only trust list", diagnostics)
        self.assertIn('trustSource === "session"', diagnostics)
        self.assertIn("navigator.clipboard.writeText(command)", diagnostics)
        self.assertIn(
            'button.textContent = "Command copied — run it in Terminal"',
            diagnostics,
        )
        self.assertIn(
            'on(els.candidateTryBody, "click", copyCandidatePreviewTrustCommand);',
            self.source,
        )
        self.assertIn("renderCandidatePreviewProfileDiagnostics(mode)", sheet)

    def test_focused_candidate_actions_use_one_compact_responsive_toolbar(
        self,
    ) -> None:
        actions = _function_source(
            self.source,
            "renderFocusedCandidateActions",
            "renderFocusedCandidateMore",
        )
        toolbar_styles = self.styles[
            self.styles.index(".candidate-focused-actions {") : self.styles.index(
                ".candidate-try-refresh-notice"
            )
        ]
        narrow_styles = self.styles[self.styles.index("@media (max-width: 520px)") :]

        self.assertIn('aria-labelledby="candidate-actions-title"', actions)
        self.assertIn('id="candidate-actions-title">Candidate actions', actions)
        self.assertIn('class="candidate-focused-action-toolbar"', actions)
        self.assertLess(
            actions.index('data-try-candidate="'),
            actions.index("renderCandidateReviewAction(item)"),
        )
        self.assertLess(
            actions.index("renderCandidateReviewAction(item)"),
            actions.index("renderCandidateComparisonAction(item, page)"),
        )
        self.assertIn("const showTryStatus = pending || !eligibleModes.length", actions)
        self.assertIn("${showTryStatus ?", actions)
        self.assertNotIn("Trying, saving, or editing", actions)

        self.assertIn("display: grid;", toolbar_styles)
        self.assertIn("gap: 12px;", toolbar_styles)
        self.assertIn(".candidate-focused-action-toolbar", toolbar_styles)
        self.assertIn("display: flex;", toolbar_styles)
        self.assertIn("flex-wrap: wrap;", toolbar_styles)
        self.assertNotIn("minmax(230px, 1fr)", toolbar_styles)
        self.assertIn("position: absolute;", toolbar_styles)
        self.assertIn("flex-direction: column;", narrow_styles)
        self.assertIn("width: 100%;", narrow_styles)

    def test_candidate_try_discloses_the_exact_read_only_plan(self) -> None:
        capability = _function_source(
            self.source,
            "actionCapability",
            "renderSelectionActions",
        )
        plan = _function_source(
            self.source,
            "renderCandidateInspectionPlan",
            "closeCandidateTrySheet",
        )
        summary = _function_source(
            self.source,
            "renderOperatorJobSummary",
            "renderOperatorJobReviewAction",
        )

        self.assertIn("inspection_plan:", capability)
        self.assertIn("optpilot.candidate-try-plan.v1", plan)
        self.assertIn("Settings for this try", plan)
        self.assertIn("Environment version", plan)
        self.assertIn("<dt>Seed</dt>", plan)
        self.assertIn('settings.seed === null', plan)
        self.assertIn('"Not set"', plan)
        self.assertIn("<dt>Repetition</dt>", plan)
        self.assertIn("<dt>Interface profile</dt>", plan)
        self.assertIn('aria-label="Settings for this try"', plan)
        self.assertIn(
            "renderCandidateInspectionPlan(job.inspection_plan",
            summary,
        )
        self.assertIn(".candidate-try-plan", self.styles)

    def test_candidate_try_rejects_a_stale_interface_profile_choice(self) -> None:
        selector = _function_source(
            self.source,
            "selectedPreviewProfileId",
            "workbenchActionKey",
        )
        sheet = _function_source(
            self.source,
            "renderCandidateTrySheet",
            "updateCandidateTrySheet",
        )
        perform = _async_function_source(
            self.source,
            "performWorkbenchAction",
            "addCandidateToReview",
        )

        self.assertIn("eligibleIds", selector)
        self.assertIn("eligibleIds.has", selector)
        self.assertIn(
            "selectedPreviewProfileId(\n    selectedMode",
            sheet,
        )
        self.assertIn(
            "selectedPreviewProfileId(capability, selectionId)",
            perform,
        )

    def test_candidate_try_refreshes_stale_run_context_without_leaving_candidate(
        self,
    ) -> None:
        confirm = _function_source(
            self.source,
            "confirmCandidateTry",
            "restoreFocusedCandidateTryFocus",
        )

        self.assertIn("if (!contextMatches)", confirm)
        self.assertIn("state.candidateTryNotice", confirm)
        self.assertIn("closeCandidateTrySheet({ restoreFocus: false })", confirm)
        self.assertIn("renderRunDetail()", confirm)
        self.assertIn(
            "loadRunDetail(refreshRunId, { keepTab: true, skipListRender: true })",
            confirm,
        )
        self.assertGreaterEqual(
            confirm.count("selectedCanonicalRunId() !== refreshRunId"),
            2,
        )
        self.assertIn("review them, then try again", confirm)
        self.assertIn(
            'restoreFocusedCandidateTryFocus(refreshSelectionId, "notice")',
            confirm,
        )
        self.assertIn("Reload this Candidate, then try again", confirm)

    def test_candidate_try_rows_keep_their_native_button_role(self) -> None:
        panel = _function_source(
            self.source,
            "operatorJobsPanelBody",
            "renderOperatorJobRow",
        )
        row = _function_source(
            self.source,
            "renderOperatorJobRow",
            "operatorJobLabel",
        )

        self.assertIn('role="group"', panel)
        self.assertIn('aria-label="Candidate tries"', panel)
        self.assertIn('type="button"', row)
        self.assertNotIn('role="listitem"', row)

    def test_focused_candidate_auto_inspects_and_keeps_re_evaluation_under_more(
        self,
    ) -> None:
        ensure = _function_source(
            self.source,
            "ensureFocusedCandidateInspection",
            "currentWorkbenchItemForCandidate",
        )
        more = _function_source(
            self.source,
            "renderFocusedCandidateMore",
            "ensureFocusedCandidateInspection",
        )

        self.assertIn('actionCapability(item, page, "inspect")', ensure)
        self.assertIn('performWorkbenchAction("inspect", selectionId)', ensure)
        self.assertIn("state.workbenchActionErrors[key]", ensure)
        self.assertIn('<summary>More actions and details</summary>', more)
        self.assertIn("Re-evaluate in a new Run", more)
        self.assertIn("Discuss in ${escapeHtml(assistantSessionLabel())}", more)
        self.assertIn("renderSelectionTechnicalDetails(selection, item)", more)

    def test_environment_preview_reuses_generic_output_cards_and_callbacks(
        self,
    ) -> None:
        outputs = _function_source(
            self.source,
            "renderOperatorJobInterfaceOutputs",
            "renderOperatorJobResult",
        )
        binding = _function_source(
            self.source,
            "bindInterfaceOutputControls",
            "bindComponentInterfaceLaunchControls",
        )
        events = _function_source(
            self.source,
            "bindOperatorJobEvents",
            "runTabContent",
        )
        keep = self.source[
            self.source.index("async function keepOperatorJobOutput(") : self.source.index(
                "function operatorJobIsActive("
            )
        ]
        view = self.source[
            self.source.index("async function viewOperatorJobOutput(") : self.source.index(
                "async function retryOperatorJobOutput("
            )
        ]

        self.assertIn("renderInterfaceOutputList(outputs)", outputs)
        self.assertIn('lifecycle === "retained"', outputs)
        self.assertIn("View read-only result files saved with this try", outputs)
        self.assertIn('retained: bundle.lifecycle === "retained"', self.source)
        self.assertIn('"Saved result"', self.source)
        self.assertIn('"Saved result · Ready as Workspace"', self.source)
        self.assertNotIn("DEVS", outputs)
        self.assertIn("callbacks.view", binding)
        self.assertIn(".interface-output-view", binding)
        self.assertIn("callbacks.keep || keepInterfaceOutput", binding)
        self.assertIn("callbacks.retry || retryInterfaceOutput", binding)
        self.assertIn("viewOperatorJobOutput(jobId, outputId)", events)
        self.assertIn("keepOperatorJobOutput(jobId, outputId)", events)
        self.assertIn("retryOperatorJobOutput(jobId, outputId)", events)
        self.assertIn(
            "/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/view",
            view,
        )
        self.assertIn(
            'schema: "optpilot.operator-job-output-content-view-request.v1"',
            view,
        )
        self.assertIn("await openSelectionContentView(", view)
        self.assertIn("requireExactHead: false", view)
        self.assertIn('"View result"', self.source)
        self.assertIn(
            "/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/keep",
            self.source,
        )
        self.assertIn(
            "/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/retry",
            self.source,
        )
        self.assertIn("{ request_id: requestId }", keep)

    def test_catalog_interface_preview_stays_transient_and_stoppable(self) -> None:
        poll = _async_function_source(
            self.source,
            "pollComponentInterfaceLaunch",
            "retryInterfaceOutput",
        )
        stop = _async_function_source(
            self.source,
            "stopComponentInterface",
            "launchWorkspaceInterface",
        )
        status = _function_source(
            self.source,
            "interfaceLaunchStatus",
            "workspaceInterfaceLaunchStatus",
        )
        workbench = _function_source(
            self.source,
            "renderCatalogInterfaceWorkbench",
            "renderCatalogInterfaceLaunchPanel",
        )
        detail = _function_source(
            self.source,
            "renderComponentDetail",
            "componentEditableWorkspaceCapability",
        )

        self.assertIn(
            'if (!previewUrl) throw new Error("Interface launch completed without a Preview URL.");',
            poll,
        )
        self.assertNotIn("mergeUiWorkspace", poll)
        self.assertNotIn("attachWorkspaceToCurrent", poll)
        self.assertNotIn('class="interface-launch-preview"', status)
        self.assertNotIn('class="interface-launch-preview"', self.source)
        self.assertIn("els.workspacePreviewFrame.src = previewUrl", workbench)
        self.assertIn("exact published Catalog version", workbench)
        self.assertNotIn("interfaceLaunchStatus(component, launchState)", detail)
        self.assertIn("component-stop-interface", status)
        self.assertIn("compactInterfaceLaunchStatus({", status)
        self.assertIn(
            "/api/interface-launches/${encodeURIComponent(launch.launch_id)}/stop", stop
        )
        self.assertIn("state.interfaceLaunch = null;", stop)

    def test_catalog_interface_outputs_are_generic_status_cards(self) -> None:
        output_list = _function_source(
            self.source,
            "renderInterfaceOutputs",
            "renderInterfaceOutputCard",
        )
        output_card = _function_source(
            self.source,
            "renderInterfaceOutputCard",
            "mergeInterfaceLaunchPayload",
        )

        self.assertIn("launchState.result.outputs", output_list)
        self.assertIn('if (!outputs.length) return "";', output_list)
        self.assertIn('aria-label="Outputs"', output_list)
        self.assertIn('data-interface-drawer-panel="outputs"', output_list)
        self.assertIn('rawKind === "tree" ? "folder"', output_card)
        self.assertIn('rawKind === "blob" ? "file"', output_card)
        self.assertNotIn("Generated projects", output_list)
        self.assertIn('if (normalized === "ready") return "Ready";', self.source)
        self.assertIn('if (normalized === "failed") return "Failed";', self.source)
        self.assertIn('return "Preparing";', self.source)
        self.assertIn("Boolean(keepAction.eligible)", output_card)
        self.assertIn("keepAction.supported === false", output_card)
        self.assertIn('class="ghost-button interface-output-keep"', output_card)
        self.assertIn("Save as Workspace", output_card)
        self.assertIn("keepAction.reason", output_card)
        self.assertIn("viewAction.eligible", output_card)
        self.assertIn(
            "This immutable output cannot be opened as an editable workspace.",
            output_card,
        )
        self.assertNotIn("DEVS", output_list + output_card)

    def test_interface_output_actions_are_generic_and_disable_while_running(
        self,
    ) -> None:
        output_card = _function_source(
            self.source,
            "renderInterfaceOutputCard",
            "mergeInterfaceLaunchPayload",
        )
        binding = _function_source(
            self.source,
            "bindInterfaceOutputControls",
            "bindComponentInterfaceLaunchControls",
        )
        execute = _async_function_source(
            self.source,
            "runInterfaceOutputAction",
            "keepInterfaceOutput",
        )

        self.assertIn("actions.execute", output_card)
        self.assertIn("executeAction.items", output_card)
        self.assertIn("activeExecutionActionIds", output_card)
        self.assertIn('"Running…"', output_card)
        self.assertIn("interface-output-execute", output_card)
        self.assertIn("item.accepts_arguments", output_card)
        self.assertIn("Optional arguments", output_card)
        self.assertIn("One argument per non-empty line", output_card)
        self.assertIn("callbacks.execute || runInterfaceOutputAction", binding)
        self.assertIn("button.dataset.actionId", binding)
        self.assertIn("interfaceOutputArgumentsFromControl(button)", binding)
        self.assertIn(
            "/outputs/${encodeURIComponent(outputId)}/actions/${encodeURIComponent(actionId)}/run",
            execute,
        )
        self.assertIn(
            'schema_version: "optpilot.interface-output-action-run-request.v1"',
            execute,
        )
        self.assertIn(
            "arguments: Array.isArray(argumentsList) ? argumentsList : []",
            execute,
        )
        self.assertNotIn("command:", execute)
        self.assertNotIn("image:", execute)
        self.assertNotIn("env:", execute)

    def test_interface_output_action_result_files_have_bounded_open_links(
        self,
    ) -> None:
        execution = _function_source(
            self.source,
            "renderInterfaceOutputExecution",
            "renderInterfaceOutputCard",
        )

        self.assertIn("result.result_file_count", execution)
        self.assertIn("resultFiles.slice(0, 6)", execution)
        self.assertIn("access.preview_eligible", execution)
        self.assertIn("access.open_url", execution)
        self.assertIn("access.download_url", execution)
        self.assertIn('rel="noopener"', execution)

    def test_first_async_output_adds_the_compact_drawer_without_losing_polling(self) -> None:
        updater = _function_source(
            self.source,
            "updateInterfaceOutputPanel",
            "interfaceOutputFocusDescriptor",
        )
        disclosure = _function_source(
            self.source,
            "captureInterfaceLaunchDisclosureState",
            "setInterfaceLaunchDrawer",
        )
        panel_render = _function_source(
            self.source,
            "renderCatalogInterfaceLaunchPanel",
            "renderCatalogInterfaceProfileSelector",
        )

        self.assertIn("Boolean(panel) !== Boolean(outputs.length)", updater)
        self.assertIn("renderInterfaceLaunchSurface(launchState)", updater)
        self.assertIn("list.innerHTML = renderInterfaceOutputList(outputs)", updater)
        self.assertIn("bindInterfaceOutputControls(surface)", updater)
        self.assertIn("outputToggle.textContent = `Outputs (${outputs.length})`", updater)
        self.assertNotIn("panel.outerHTML", updater)
        self.assertIn("openPanel", disclosure)
        self.assertIn("captureInterfaceLaunchDisclosureState(surface)", panel_render)
        self.assertIn("restoreInterfaceLaunchDisclosureState", panel_render)

    def test_interface_outputs_use_the_shared_read_only_content_viewer(self) -> None:
        binding = _function_source(
            self.source,
            "bindInterfaceOutputControls",
            "bindComponentInterfaceLaunchControls",
        )
        view = _async_function_source(
            self.source,
            "viewInterfaceOutput",
            "retryInterfaceOutput",
        )
        host = _function_source(
            self.source,
            "renderSelectionContentHost",
            "renderSelectionContentDrawer",
        )

        self.assertIn("callbacks.view || viewInterfaceOutput", binding)
        self.assertIn(
            "/api/interface-launches/${encodeURIComponent(launchId)}/outputs/${encodeURIComponent(outputId)}/view",
            view,
        )
        self.assertIn(
            'schema: "optpilot.interface-output-content-view-request.v1"',
            view,
        )
        self.assertIn("await openSelectionContentView(", view)
        self.assertIn("renderSelectionContentDrawer()", host)
        self.assertIn('id="selectionContentDrawerHost"', self.html)

    def test_catalog_interface_keep_preserves_card_and_opens_returned_workspace(
        self,
    ) -> None:
        merge = _function_source(
            self.source,
            "mergeInterfaceLaunchPayload",
            "updateInterfaceOutput",
        )
        binding = _function_source(
            self.source,
            "bindInterfaceOutputControls",
            "bindComponentInterfaceLaunchControls",
        )
        keep = _async_function_source(
            self.source,
            "keepInterfaceOutput",
            "stopComponentInterface",
        )

        self.assertIn(
            "kept_workspace_id: output && output.kept_workspace_id || local.kept_workspace_id",
            merge,
        )
        self.assertIn('keep_error: serverWorkspaceId ? "" : local.keep_error', merge)
        self.assertIn("keep_request_id: local.keep_request_id", merge)
        self.assertIn("keep_state: output && output.keep_state", merge)
        self.assertIn(".interface-output-open", binding)
        self.assertIn("callbacks.open || selectSession", binding)
        self.assertIn("openWorkspace(button.dataset.workspaceId)", binding)
        self.assertIn(".interface-output-curate", binding)
        self.assertIn("callbacks.curate || openWorkspaceForCuration", binding)
        self.assertIn("curateWorkspace(button.dataset.workspaceId)", binding)
        self.assertIn(">Publish</button>", self.html)
        self.assertIn(
            "Set up for Catalog opens this Workspace's publishing steps",
            self.source,
        )
        self.assertIn("await selectSession(workspaceId);", binding)
        self.assertIn("await openRegistrationMenu();", binding)
        self.assertIn("/outputs/${encodeURIComponent(outputId)}/keep", keep)
        self.assertIn("{ request_id: requestId }", keep)
        self.assertIn("const workspace = mergeUiWorkspace(payload.workspace);", keep)
        self.assertIn("kept_workspace_id: workspace.id", keep)
        self.assertIn("kept_workspace_title: workspace.title", keep)

    def test_catalog_interface_launch_resumes_after_browser_refresh(self) -> None:
        resume = _async_function_source(
            self.source,
            "resumeStoredInterfaceLaunch",
            "launchComponentInterface",
        )
        merge = _function_source(
            self.source,
            "mergeInterfaceLaunchPayload",
            "updateInterfaceOutput",
        )
        recovery = _function_source(
            self.source,
            "fetchInterfaceLaunchStatusWithRecovery",
            "handleInterfaceLaunchPollingError",
        )

        self.assertIn("activeInterfaceLaunch", self.source)
        self.assertIn(
            "fetchInterfaceLaunchStatusWithRecovery(launchKey, launchId)",
            resume,
        )
        self.assertIn("/api/interface-launches/${encodeURIComponent(launchId)}", recovery)
        self.assertIn("pollComponentInterfaceLaunch(launchKey, launchId)", resume)
        self.assertIn("pollWorkspaceInterfaceLaunch(launchKey, launchId)", resume)
        self.assertIn("await loadUiWorkspaces()", resume)
        self.assertIn("rebuildDerivedState()", resume)
        self.assertIn("persistActiveInterfaceLaunch(merged)", merge)

    def test_catalog_is_exact_read_only_and_workspace_creation_is_explicit(self) -> None:
        catalog = _async_function_source(
            self.source,
            "openComponentSession",
            "launchComponentInterface",
        )
        study = _function_source(self.source, "planPayload", "exactCatalogEntryRef")
        detail = _function_source(
            self.source,
            "renderComponentDetail",
            "componentEditableWorkspaceCapability",
        )
        profiles = _function_source(
            self.source,
            "componentInterfaceProfiles",
            "componentSelectedInterfaceProfile",
        )
        selection = _function_source(
            self.source,
            "selectedInterfaceProfile",
            "componentInterfaceProfiles",
        )
        launch = _async_function_source(
            self.source,
            "launchComponentInterface",
            "pollComponentInterfaceLaunch",
        )

        self.assertIn("state.catalogWorkspaceRequestIds[requestKey] = newRequestId()", catalog)
        self.assertIn("requestPayload.request_id = state.catalogWorkspaceRequestIds[requestKey]", catalog)
        self.assertNotIn("requestPayload.config", catalog)
        self.assertNotIn("session_id", catalog)
        self.assertIn('const requestPayload = {};', catalog)
        self.assertIn('saveAsDraft ? "draftSaveRequestId" : "launchPreparationRequestId"', study)
        self.assertIn("if (!plan[requestKey]) plan[requestKey] = newRequestId()", study)
        self.assertIn("payload.request_id = plan[requestKey]", study)
        self.assertIn("payload.draft_action_id = plan.draftActionId", study)
        self.assertIn("editableCapability.eligible !== true", detail)
        self.assertIn("component-edit-guidance", detail)
        self.assertIn("interfaceCapability.eligible !== true", detail)
        self.assertIn("component-interface-guidance", detail)
        self.assertIn("openComponentInterface(component)", detail)
        self.assertIn("const editLabel =", detail)
        self.assertIn("linkedWorkspaceId", detail)
        self.assertIn('"Open Workspace"', detail)
        self.assertIn('"Edit in Workspace"', detail)
        self.assertNotIn("componentConfigEditor(component)", detail)
        self.assertIn("bindComponentReadOnlyControls()", detail)
        self.assertIn("catalog_source_unpublished", self.source)
        self.assertIn("summarizedInterfaceProfiles", profiles)
        self.assertNotIn("raw_config", profiles)
        self.assertIn("eligibleProfiles", selection)
        self.assertIn("eligibleProfiles.length ? eligibleProfiles : profiles", selection)
        self.assertIn("profile_id: profile.id", launch)
        self.assertIn(
            "componentInterfaceLaunchCapability(component, profile)", launch
        )
        self.assertIn("if (capability.eligible !== true) {", launch)
        self.assertIn("error: String(capability.reason", launch)
        self.assertNotIn("config:", launch)
        self.assertNotIn("Save Editable Workspace", self.source)
        self.assertNotIn("componentConfigDraft", self.source)

    def test_assistant_catalog_interface_action_returns_to_the_active_interface(self) -> None:
        current = _function_source(
            self.source,
            "assistantUiCardCurrentActionState",
            "assistantUiCardLatestEventIndexes",
        )
        html = _function_source(
            self.source,
            "assistantUiCardsHtml",
            "bindAssistantUiCards",
        )
        execute = _async_function_source(
            self.source,
            "executeAssistantUiCardAction",
            "resolveAssistantUiCardWorkspace",
        )

        self.assertIn("isActiveInterfaceLaunch(state.interfaceLaunch)", current)
        self.assertIn("`Return to ${String(state.interfaceLaunch.label", current)
        self.assertIn("currentAction.label || action.label", html)
        self.assertIn("openActiveInterfaceLocation()", execute)
        self.assertNotIn("Open it from Open work or stop it", execute)

    def test_catalog_interface_poll_keeps_live_output_status_fresh(self) -> None:
        poll = _async_function_source(
            self.source,
            "pollComponentInterfaceLaunch",
            "retryInterfaceOutput",
        )
        panel_update = _function_source(
            self.source,
            "updateInterfaceOutputPanel",
            "bindInterfaceOutputControls",
        )

        self.assertIn(
            "mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey)",
            poll,
        )
        self.assertIn("await sleep(1000);\n      continue;", poll)
        self.assertIn("state.interfaceLaunch.launch_id !== launchId", poll)
        self.assertIn("else updateInterfaceOutputPanel(state.interfaceLaunch);", poll)
        self.assertIn("panel.dataset.outputSignature === signature", panel_update)
        self.assertIn(
            "list.innerHTML = renderInterfaceOutputList(outputs);", panel_update
        )
        self.assertNotIn("renderComponentDetail", panel_update)

    def test_failed_interface_output_retry_is_explicit(self) -> None:
        output_card = _function_source(
            self.source,
            "renderInterfaceOutputCard",
            "mergeInterfaceLaunchPayload",
        )
        poll = _async_function_source(
            self.source,
            "pollComponentInterfaceLaunch",
            "retryInterfaceOutput",
        )
        retry = _async_function_source(
            self.source,
            "retryInterfaceOutput",
            "keepInterfaceOutput",
        )

        self.assertIn('class="ghost-button interface-output-retry"', output_card)
        self.assertIn("Try again", output_card)
        self.assertIn("output.failure_reason", output_card)
        self.assertNotIn("errorCode", output_card)
        self.assertIn("/outputs/${encodeURIComponent(outputId)}/retry", retry)
        self.assertIn('status: returned.status || "sealing"', retry)
        self.assertNotIn("retryInterfaceOutput(", poll)

    def test_run_overview_uses_complete_candidate_summary_and_bounded_series(
        self,
    ) -> None:
        detail = _function_source(
            self.source,
            "renderRunDetail",
            "selectRunActionContext",
        )
        overview = _function_source(
            self.source,
            "runOverview",
            "runProviderCapabilityNote",
        )
        metric_panel = _function_source(
            self.source,
            "runCompleteObjectivePanel",
            "runOverview",
        )

        self.assertIn("runHeadlineResult(detail)", detail)
        self.assertIn("Trial progress", detail)
        self.assertIn("Complete Candidates", detail)
        self.assertIn("activeTechnicalTab", detail)
        self.assertIn('activeTechnicalTab ? "open" : ""', detail)
        self.assertIn("Technical evidence${activeTechnicalTab", detail)
        self.assertIn("const onlyCompleteCandidate", overview)
        self.assertIn("Open only complete Candidate", overview)
        self.assertIn("best.candidateId || headlineResult.candidateId", overview)
        self.assertIn(
            '["Complete Candidate value", headlineResult.candidateId ? headlineResult.value : "-"]',
            overview,
        )
        self.assertIn("headlineResult.sampleCount", overview)
        self.assertIn(
            '["Failures requiring attention", (overview && overview.failure_count)',
            overview,
        )
        self.assertIn("runCompleteObjectivePanel(detail)", overview)
        self.assertIn("overview.objective_series", metric_panel)
        self.assertIn("series.total_complete_candidates", metric_panel)
        self.assertIn("These results come from this Run", metric_panel)
        self.assertIn("The one complete Candidate is shown.", metric_panel)
        self.assertIn('class="run-metric-chart"', metric_panel)
        self.assertIn('class="run-metric-table"', metric_panel)
        self.assertIn("View complete Candidate values as a table", metric_panel)
        self.assertIn("series.truncated", metric_panel)
        self.assertNotIn('workbenchPage(detail, "observation")', metric_panel)
        self.assertNotIn("page.page.has_more", metric_panel)
        self.assertIn('class="run-result-state', overview)
        self.assertIn('class="run-technical-details"', overview)
        self.assertNotIn("best.observationId", overview)
        self.assertNotIn("best.trialId", overview)

    def test_candidate_inspection_does_not_claim_current_try_availability(self) -> None:
        inspection = _function_source(
            self.source,
            "renderCandidateInspection",
            "semanticPanel",
        )

        self.assertIn('"Environment evaluator"', inspection)
        self.assertIn('"Configured"', inspection)
        self.assertNotIn('"Can be tried"', inspection)

    def test_overview_uses_exact_head_core_comparability_without_another_read(
        self,
    ) -> None:
        coherence = _function_source(
            self.source,
            "coherentRunDetail",
            "runWorkbenchTabs",
        )
        overview = _function_source(
            self.source,
            "runOverview",
            "runComparabilityPanel",
        )
        panel = _function_source(
            self.source,
            "runComparabilityPanel",
            "runComparabilityFingerprint",
        )
        fingerprint = _function_source(
            self.source,
            "runComparabilityFingerprint",
            "runComparabilityStatusClass",
        )

        self.assertIn("detail.workbench.comparability", coherence)
        self.assertIn(
            'comparability.schema !== "optpilot.run-comparability-projection.v1"',
            coherence,
        )
        self.assertIn(
            "!sameRunHead(comparability.head, detail.workbench.head)", coherence
        )
        self.assertIn(
            'overview.schema !== "optpilot.run-overview-projection.v1"',
            coherence,
        )
        self.assertIn("!sameRunHead(overview.head, detail.workbench.head)", coherence)
        self.assertIn("runComparabilityPanel(detail.workbench.comparability)", overview)
        self.assertIn("fingerprints.environment_evaluation", panel)
        self.assertIn("fingerprints.objective", panel)
        self.assertIn("environment.method_identity_included", panel)
        for dimension in (
            '"semantic_inputs"',
            '"bytes_available_now"',
            '"runtime_identity"',
            '"runtime_available_now"',
            '"isolation"',
            '"external_replayability"',
            '"seed_repetition_plan"',
            '"terminal_evidence"',
        ):
            self.assertIn(dimension, panel)
        self.assertIn("Why automatic cross-run ranking is unavailable", panel)
        self.assertIn("automaticRanking.blocking_reasons", panel)
        self.assertIn(
            "Matching fingerprints alone do not establish reproducible comparability",
            panel,
        )
        self.assertIn("/^[0-9a-f]{64}$/.test(digest)", fingerprint)
        combined = coherence + overview + panel + fingerprint
        self.assertNotIn("fetch(", combined)
        self.assertNotIn("postJson(", combined)
        self.assertNotIn("crypto", combined)
        self.assertNotIn("/comparability", combined)
        self.assertNotIn("content_ref", combined)
        self.assertNotIn("owner_id", combined)

    def test_comparability_panel_has_compact_responsive_styles(self) -> None:
        for selector in (
            ".run-comparability-panel",
            ".run-comparability-heading",
            ".run-comparability-fingerprints",
            ".run-comparability-dimensions",
            ".run-comparability-dimension",
            ".run-comparability-ranking",
            ".run-comparability-note",
        ):
            self.assertIn(selector, self.styles)
        responsive = self.styles[self.styles.index("@container (max-width: 760px)") :]
        self.assertIn(".run-comparability-fingerprints", responsive)
        self.assertIn(".run-comparability-dimension", responsive)

    def test_candidate_page_uses_server_projected_results_and_exact_empty_states(
        self,
    ) -> None:
        dispatch = _function_source(
            self.source,
            "renderWorkbenchPage",
            "renderCandidateResultsPage",
        )
        page = _function_source(
            self.source,
            "renderCandidateResultsPage",
            "workbenchRunIsTerminal",
        )
        candidate_slice = self.source[
            self.source.index(
                "function renderCandidateResultsPage("
            ) : self.source.index("function renderIndividualObservationsPage(")
        ]

        self.assertIn(
            'if (kind === "candidate") return renderCandidateResultsPage(detail, page);',
            dispatch,
        )
        self.assertIn(
            'if (kind === "observation") return renderIndividualObservationsPage(detail, page);',
            dispatch,
        )
        self.assertIn("page.capabilities && page.capabilities.candidate_results", page)
        self.assertIn("page.result_summary || page.candidate_result_summary", page)
        self.assertIn("candidateRankingContext(page)", page)
        self.assertIn("Candidate results", page)
        self.assertIn(
            "Candidate summaries are temporarily unavailable. Recorded trial results are still available under Technical evidence.",
            page,
        )
        self.assertIn("Waiting for the Method to submit its first Candidate.", page)
        self.assertIn("Run ended before any candidates were accepted.", page)
        self.assertIn("data.result", candidate_slice)
        self.assertIn("aggregate.value", candidate_slice)
        self.assertIn("counts.usable_objectives", candidate_slice)
        self.assertIn("comparison.ranked_candidate_count", candidate_slice)
        self.assertIn("comparison.group_ordinal", candidate_slice)
        self.assertIn("comparison.scope", candidate_slice)
        self.assertNotIn("Object.entries", candidate_slice)
        self.assertNotIn(".reduce(", candidate_slice)
        self.assertNotIn("objective_value", candidate_slice)
        self.assertNotIn("candidate_ref", candidate_slice)
        self.assertNotIn("content_ref", candidate_slice)

    def test_candidate_rows_present_completion_coverage_rank_and_failures(self) -> None:
        status = _function_source(
            self.source,
            "candidateResultStatusPresentation",
            "candidateResultReason",
        )
        row = _function_source(
            self.source,
            "renderCandidateResultItem",
            "renderIndividualObservationsPage",
        )

        for value in (
            'label: "Complete"',
            'label: "Evaluating"',
            'label: "Incomplete"',
            'label: "No usable result"',
            'label: "Not comparable"',
        ):
            self.assertIn(value, status)
        self.assertIn('result.status === "rankable"', status)
        self.assertIn('result.status === "aggregate_only"', status)
        self.assertLess(
            status.index("objective_aggregation_not_supported"),
            status.index("candidate_evaluation_active"),
        )
        self.assertLess(
            status.index('reason === "terminal_result_not_successful"'),
            status.index('reason === "candidate_evaluation_active"'),
        )
        self.assertIn('rankingContext.finality === "provisional_at_head"', row)
        self.assertIn("Provisional", row)
        self.assertIn("Objective coverage", row)
        self.assertIn("candidate-result-error", row)
        self.assertIn("candidate-result-warning", row)
        self.assertIn('<button class="workbench-entity candidate-result-row candidate-result-link"', row)
        self.assertIn("data-open-candidate-route", row)
        self.assertNotIn("<details", row)
        self.assertNotIn("renderSpecializedWorkbenchBody", row)

    def test_candidate_compare_is_generic_two_click_and_server_projected(self) -> None:
        selection = _function_source(
            self.source,
            "candidateComparisonSelection",
            "renderCandidateComparisonAction",
        )
        action = _function_source(
            self.source,
            "renderCandidateComparisonAction",
            "renderCandidateComparisonPanel",
        )
        focused_actions = _function_source(
            self.source,
            "renderFocusedCandidateActions",
            "renderFocusedCandidateMore",
        )
        choose = _function_source(
            self.source,
            "chooseCandidateComparison",
            "requestCandidateComparison",
        )
        request_start = self.source.index("async function requestCandidateComparison(")
        request_end = self.source.index(
            "function swapCandidateComparison(", request_start
        )
        request = self.source[request_start:request_end]
        row = _function_source(
            self.source,
            "renderCandidateResultItem",
            "renderIndividualObservationsPage",
        )

        self.assertIn('item.kind === "candidate"', selection)
        self.assertIn('actionCapability(item, page, "compare")', selection)
        self.assertIn("capability.eligible", selection)
        self.assertNotIn('data.format === "parameters"', selection)
        self.assertIn("presentation_selection: { ...selection }", selection)
        self.assertIn("data-candidate-compare=", action)
        self.assertIn('? "Compare with baseline"', action)
        self.assertIn("if (!baseline)", choose)
        self.assertIn("state.candidateComparisonBaseline = candidate;", choose)
        self.assertIn("requestCandidateComparison(baseline, candidate);", choose)
        self.assertIn("/candidate-comparison`, {", request)
        self.assertIn('schema: "optpilot.run-candidate-comparison-request.v2"', request)
        self.assertIn("baseline_selection: baseline.presentation_selection", request)
        self.assertIn("comparison_selection: candidate.presentation_selection", request)
        self.assertIn("text_diff_path: textDiffPath", request)
        self.assertIn(
            'projection.schema !== "optpilot.run-candidate-comparison.v3"', request
        )
        self.assertIn("${renderCandidateComparisonAction(item, page)}", focused_actions)
        self.assertNotIn("renderCandidateComparisonAction", row)

    def test_candidate_comparison_panel_renders_independent_server_projections(
        self,
    ) -> None:
        panel = _function_source(
            self.source,
            "renderCandidateComparisonPanel",
            "renderCandidateComparisonProjection",
        )
        projection = _function_source(
            self.source,
            "renderCandidateComparisonProjection",
            "renderCandidateOutcomeComparison",
        )
        outcomes = _function_source(
            self.source,
            "renderCandidateOutcomeComparison",
            "renderCandidateOutcomeMetricRow",
        )
        metric_row = _function_source(
            self.source,
            "renderCandidateOutcomeMetricRow",
            "renderCandidateOutcomeMetricCell",
        )
        metric_cell = _function_source(
            self.source,
            "renderCandidateOutcomeMetricCell",
            "renderCandidateConstraintComparison",
        )
        constraints = _function_source(
            self.source,
            "renderCandidateConstraintComparison",
            "renderCandidateInputComparison",
        )
        candidate_input = _function_source(
            self.source,
            "renderCandidateInputComparison",
            "renderCandidateComparisonRow",
        )
        row = _function_source(
            self.source,
            "renderCandidateComparisonRow",
            "renderCandidateComparisonCell",
        )
        cell = _function_source(
            self.source,
            "renderCandidateComparisonCell",
            "clearCandidateComparison",
        )
        comparison_slice = self.source[
            self.source.index(
                "function renderCandidateComparisonPanel("
            ) : self.source.index("function candidateAggregationLabel(")
        ]

        self.assertIn("Candidate comparison", panel)
        self.assertIn("Recorded outcomes and Candidate inputs", panel)
        self.assertIn("candidate-comparison-swap", panel)
        self.assertIn("candidate-comparison-clear", panel)
        self.assertIn("projection.eligibility", projection)
        self.assertIn("projection.operands", projection)
        self.assertIn("projection.outcomes", projection)
        self.assertIn("projection.candidate_input", projection)
        self.assertIn("outcomes.evaluation_plan", outcomes)
        self.assertIn("outcomes.metrics", outcomes)
        self.assertIn("metrics.rows", outcomes)
        self.assertIn("coordinate_count", outcomes)
        self.assertNotIn(".digest", outcomes)
        for field in (
            "row.baseline",
            "row.comparison",
            "relation.numeric",
            "relation.delta",
            "relation.delta_semantics",
            "relation.preferred_operand",
        ):
            self.assertIn(field, metric_row)
        self.assertIn("cell.aggregate", metric_cell)
        self.assertIn("cell.coverage", metric_cell)
        self.assertIn("coverage.planned", metric_cell)
        self.assertIn("coverage.usable", metric_cell)
        self.assertIn("constraints.eligibility", constraints)
        self.assertIn("Constraint comparison unavailable", constraints)
        self.assertIn("constraints.rows", constraints)
        self.assertIn("renderCandidateConstraintRow", constraints)
        self.assertIn("renderCandidateConstraintCell", constraints)
        self.assertIn("relation.preferred_operand", constraints)
        self.assertIn("coverage.satisfied", constraints)
        self.assertIn("coverage.violated", constraints)
        self.assertIn("candidateInput.eligibility", candidate_input)
        self.assertIn("candidateInput.summary", candidate_input)
        self.assertIn("candidateInput.parameters", candidate_input)
        self.assertIn("candidateInput.files", candidate_input)
        self.assertIn("candidateInput.metadata", candidate_input)
        self.assertIn("Candidate input comparison unavailable", candidate_input)
        self.assertIn("rows.map(renderCandidateComparisonRow)", candidate_input)
        self.assertIn("renderCandidateFileInputComparison", candidate_input)
        self.assertIn("renderCandidateFileComparisonRow", candidate_input)
        self.assertIn("renderCandidateFileComparisonCell", candidate_input)
        self.assertIn("renderCandidateFileTextDiff", candidate_input)
        self.assertIn("data-candidate-text-diff", candidate_input)
        self.assertIn("optpilot.candidate-file-text-diff.v1", candidate_input)
        self.assertIn("No Workspace was created", candidate_input)
        self.assertIn("both Candidate files", candidate_input)
        self.assertNotIn("candidate trees", candidate_input)
        self.assertIn("renderCandidateMetadataInputComparison", candidate_input)
        self.assertIn("renderCandidateMetadataComparisonRow", candidate_input)
        self.assertIn("row.change", row)
        self.assertIn("row.baseline", row)
        self.assertIn("row.comparison", row)
        self.assertIn("cell.included", cell)
        self.assertIn("formatCell(cell.value)", cell)
        self.assertNotIn("Object.entries", comparison_slice)
        self.assertNotIn(".reduce(", comparison_slice)
        self.assertNotIn("content_ref", comparison_slice)
        self.assertNotIn("owner_id", comparison_slice)
        self.assertNotIn("workspace_path", comparison_slice)

    def test_candidate_comparison_resets_on_run_or_exact_head_change(self) -> None:
        context = _function_source(
            self.source,
            "selectCandidateComparisonContext",
            "selectOperatorJobsRun",
        )
        load_start = self.source.index("async function loadRunDetail(")
        load_end = self.source.index("function runLineageHtml(", load_start)
        load = self.source[load_start:load_end]
        self.assertIn("state.candidateComparisonRunId === normalizedRunId", context)
        self.assertIn(
            "sameRunHead(state.candidateComparisonHead, normalizedHead)", context
        )
        self.assertIn("state.candidateComparisonRequestSeq += 1;", context)
        self.assertIn("state.candidateComparisonBaseline = null;", context)
        self.assertIn("state.candidateComparisonProjection = null;", context)
        self.assertIn(
            "selectCandidateComparisonContext(runId, detail.workbench.head);", load
        )
        self.assertNotIn("openRunWorkspace", self.source)
        self.assertNotIn("/open-workspace", self.source)

    def test_candidate_comparison_panel_is_bounded_and_responsive(self) -> None:
        for selector in (
            ".candidate-comparison-panel",
            ".candidate-comparison-heading",
            ".candidate-comparison-section",
            ".candidate-comparison-section-heading",
            ".candidate-comparison-operands",
            ".candidate-comparison-plan",
            ".candidate-comparison-summary",
            ".candidate-comparison-table-wrap",
            ".candidate-comparison-table",
            ".candidate-outcome-table",
            ".candidate-outcome-cell",
            ".candidate-constraint-comparison",
            ".candidate-constraint-table",
            ".candidate-file-comparison-facts",
            ".candidate-file-comparison-table",
            ".candidate-file-text-diff",
            ".candidate-file-text-diff-body",
            ".candidate-file-cell",
            ".candidate-metadata-comparison-table",
            ".run-observation-insights",
            ".run-observation-panel",
            ".run-metric-chart",
            ".run-constraint-grid",
        ):
            self.assertIn(selector, self.styles)
        self.assertIn("overflow-x: auto;", self.styles)
        container = self.styles[self.styles.index("@container (max-width: 760px)") :]
        self.assertIn(".candidate-comparison-heading", container)
        self.assertIn(".candidate-comparison-section-heading", container)
        self.assertIn("grid-template-columns: 1fr;", container)

    def test_candidate_rank_is_visibly_scoped_when_multiple_plans_exist(self) -> None:
        page = _function_source(
            self.source,
            "renderCandidateResultsPage",
            "workbenchRunIsTerminal",
        )
        context = _function_source(
            self.source,
            "candidateRankingContext",
            "renderCandidateResultItem",
        )
        row = _function_source(
            self.source,
            "renderCandidateResultItem",
            "renderIndividualObservationsPage",
        )

        self.assertIn("resultCapability.ranking", context)
        self.assertIn("summaryCounts.comparison_groups", context)
        self.assertIn('ranking.scope || ""', context)
        self.assertIn('"within_evaluation_plan", "within_run_evaluation_plan"', context)
        self.assertIn(
            "ranking.supported === true && ranking.eligible === true", context
        )
        self.assertIn(
            "Ranks are shown within matching trial groups; there is no overall rank.",
            page,
        )
        self.assertIn(
            'rank == null ? "Unranked" : rankingContext.multipleComparisonGroups ? "Group rank"',
            row,
        )
        self.assertIn('? "Group rank" : "Rank"', row)
        self.assertIn("Trial group ${escapeHtml(groupOrdinal", row)
        self.assertIn("comparison.ranked_candidate_count", row)
        self.assertIn("comparison.group_ordinal", row)
        self.assertIn("candidate-result-plan", row)

    def test_observation_page_is_explicit_allowlisted_evidence(self) -> None:
        page = _function_source(
            self.source,
            "renderIndividualObservationsPage",
            "renderObservationItem",
        )
        row = _function_source(
            self.source,
            "renderObservationItem",
            "renderSpecializedWorkbenchBody",
        )
        body = _function_source(
            self.source,
            "renderSpecializedWorkbenchBody",
            "renderWorkbenchItem",
        )

        self.assertIn("Trial results", page)
        self.assertIn(
            "Each row is one recorded trial result. Candidate results combine completed trials that used the same settings.",
            page,
        )
        self.assertIn("No trial results yet; evaluations are still in progress.", page)
        self.assertIn(
            "No trial results were recorded; open Trials or Trial attempts to see what happened.",
            page,
        )
        for field in (
            "data.candidate_id",
            "data.logical_trial_id",
            "data.attempt_id",
            "data.objective_metric",
            "data.objective_value",
            "data.wall_clock_seconds",
            "data.artifact_count",
        ):
            self.assertIn(field, row)
        self.assertNotIn("Object.entries", row)
        self.assertNotIn("envelope_digest", row)
        self.assertNotIn("candidate_ref", row)
        self.assertNotIn("content_ref", row)
        self.assertIn("${renderSelectionActions(item, page)}", body)
        self.assertIn("data-workbench-ask-assistant", body)
        self.assertIn("correlations.map(renderCorrelation)", body)
        self.assertIn("renderSelectionTechnicalDetails(selection, item)", body)

    def test_candidate_and_observation_rows_are_responsive(self) -> None:
        for selector in (
            ".candidate-results-list",
            ".candidate-result-summary",
            ".candidate-result-rank",
            ".candidate-result-measure",
            ".candidate-result-tags",
            ".candidate-result-plan",
            ".candidate-result-evidence",
            ".individual-observation-row",
            ".observation-summary",
            ".observation-evidence",
        ):
            self.assertIn(selector, self.styles)
        mobile = self.styles[self.styles.index("@media (max-width: 820px)") :]
        self.assertIn(".candidate-result-summary", mobile)
        self.assertIn("summary.observation-summary", mobile)
        self.assertIn("grid-template-columns: 1fr;", mobile)
        self.assertIn("container-type: inline-size", self.styles)
        self.assertIn("@container (max-width: 760px)", self.styles)
        self.assertIn(".candidate-result-link .candidate-result-summary::after", self.styles)
        self.assertIn("summary.observation-summary::after", self.styles)


if __name__ == "__main__":
    unittest.main()
