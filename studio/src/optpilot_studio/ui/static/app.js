const STORAGE_KEYS = {
  selectedAgentSessionId: "optpilot.studio.selectedAgentSessionId",
  durableActionIntents: "optpilot.studio.durableActionIntents.v1",
  durableShortlistIntents: "optpilot.studio.durableShortlistIntents.v1",
  activeInterfaceLaunch: "optpilot.studio.activeInterfaceLaunch.v1",
  activeStudyLaunch: "optpilot.studio.activeStudyLaunch.v1",
};

const SELECTION_CONTENT_TREE_PAGE_LIMIT = 100;
const SELECTION_CONTENT_TREE_ENTRY_LIMIT = 1000;
const SELECTION_CONTENT_PREVIEW_CHUNK_LIMIT = 32 * 1024;
const SELECTION_CONTENT_PREVIEW_LIMIT = 128 * 1024;

const state = {
  view: "workspace",
  workspace: null,
  runtime: null,
  codeServer: null,
  uiWorkspaces: [],
  catalog: { environments: [], methods: [], studies: [], resources: [], sources: [] },
  catalogLoaded: false,
  catalogLoading: true,
  catalogError: "",
  catalogRequestSeq: 0,
  compatibility: { pairs: [] },
  compatibilityError: "",
  runs: [],
  runCatalog: null,
  runUnavailable: null,
  sessions: [],
  agentSessions: [],
  selectedAgentSessionId: loadStoredValue(STORAGE_KEYS.selectedAgentSessionId),
  agentWorkspaceAttachments: {},
  selectedWorkspaceByAgentSession: {},
  assistantMessagesBySession: {},
  agentApprovalsBySession: {},
  assistantApprovalKeysBySession: {},
  agentEventsBySession: {},
  handledPreviewEventIds: new Set(),
  cancellingAgentSessionIds: new Set(),
  syncingAgentSessionIds: new Set(),
  agentSessionSeq: 1,
  plans: [],
  studyDrafts: [],
  selectedSessionId: null,
  selectedFileKey: null,
  selectedComponentKey: null,
  componentFilter: "all",
  componentPackageFilter: "all",
  componentSearch: "",
  configuredSourceWorkspaceActions: {},
  catalogComponentActions: {},
  catalogSourceComponents: {},
  catalogWorkspaceRequestIds: {},
  interfaceProfileSelections: {},
  interfaceOutputArgumentDrafts: new Map(),
  planSearch: "",
  selectedPlanId: null,
  selectedRunId: null,
  selectedRun: null,
  routedCandidateId: null,
  routedCandidateResolution: null,
  routedCandidateFocusApplied: "",
  runMetricSelections: {},
  assistantRunSelection: null,
  operatorJobsRunId: null,
  operatorJobs: [],
  operatorJobsLoaded: false,
  operatorJobsLoading: false,
  operatorJobsRefreshInFlight: false,
  operatorJobsError: "",
  operatorJobsRequestSeq: 0,
  selectedOperatorJobId: null,
  selectedOperatorJob: null,
  operatorJobDetailRequestSeq: 0,
  operatorJobDetailError: "",
  pendingOperatorJobStops: new Set(),
  operatorJobStopErrors: {},
  operatorJobOutputActions: {},
  workbenchActionRunId: null,
  pendingWorkbenchActions: new Set(),
  workbenchActionRequestIds: loadStoredJson(STORAGE_KEYS.durableActionIntents),
  shortlistRequestIntents: loadStoredJson(STORAGE_KEYS.durableShortlistIntents),
  workbenchActionErrors: {},
  environmentPreviewProfileSelections: {},
  semanticInspections: {},
  candidateComparisonRunId: null,
  candidateComparisonHead: null,
  candidateComparisonBaseline: null,
  candidateComparisonCandidate: null,
  candidateComparisonProjection: null,
  candidateComparisonLoading: false,
  candidateComparisonError: "",
  candidateComparisonRequestSeq: 0,
  reviewDrafts: {},
  reviewViewedCollections: {},
  reviewPendingSelectionIds: new Set(),
  reviewPendingOperatorJobIds: new Set(),
  reviewSelectionErrors: {},
  reviewOperatorJobErrors: {},
  reviewSavePending: false,
  reviewDeletePending: false,
  reviewHistoryPending: false,
  reviewError: "",
  selectionContentSessionId: "",
  selectionContentView: null,
  selectionContentTree: null,
  selectionContentPreview: null,
  selectionContentLoading: false,
  selectionContentError: "",
  selectionContentRequestSeq: 0,
  expandedWorkbenchSelections: new Set(),
  runsRefreshInFlight: false,
  runDetailRequestSeq: 0,
  runPageRequestSeq: 0,
  runPageLoadingKind: null,
  runStatusFilter: "all",
  activeRunTab: "overview",
  sessionTab: "terminal",
  workbenchMode: "code",
  assistantOpen: false,
  assistantMode: "chat",
  assistantPanelWidth: 320,
  registrationDraft: null,
  registrationNotice: null,
  workspaceNotice: null,
  registrationActionPending: "",
  embeddedCodeUrl: "",
  embeddedCodeFolder: "",
  workspacePreviews: {},
  interfaceLaunch: null,
  interfaceReturnPending: false,
  interfaceReturnError: "",
  interfaceReturnFallbackUrl: "",
  storedInterfaceLaunch: loadSessionStoredJson(STORAGE_KEYS.activeInterfaceLaunch),
  studyLaunch: null,
  storedStudyLaunch: loadStoredJson(STORAGE_KEYS.activeStudyLaunch),
  studyLaunchPollGeneration: 0,
  platformReady: false,
  codeWorkspaceStatus: "idle",
  codeWorkspaceMessage: "",
  codeWorkspacePaused: false,
  codeWorkspaceRequestSeq: 0,
  agentSettings: null,
  agentRuntimeStatus: null,
  settingsOpen: false,
  settingsTab: "assistant",
  environmentVariableDrafts: [],
  pendingWorkspaceCleanup: null,
  pendingRegistrationConfirmation: null,
  pendingCandidateTry: null,
  candidateTryNotice: "",
  candidateTryReturnFocus: null,
  pendingChildRunConfirmation: null,
  childRunReturnFocus: null,
  pendingRunStop: null,
  runStopReturnFocus: null,
  pendingInterfaceStop: null,
  interfaceStopReturnFocus: null,
  localFolderReturnFocus: null,
};

const els = {};

function loadStoredValue(key) {
  try {
    return window.localStorage.getItem(key) || null;
  } catch (error) {
    return null;
  }
}

function loadStoredJson(key) {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (error) {
    return {};
  }
}

function storeValue(key, value) {
  try {
    if (value) {
      window.localStorage.setItem(key, value);
    } else {
      window.localStorage.removeItem(key);
    }
  } catch (error) {
    // Local storage can be unavailable in restricted browser contexts.
  }
}

function loadSessionStoredJson(key) {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (error) {
    return {};
  }
}

function storeSessionValue(key, value) {
  try {
    if (value) {
      window.sessionStorage.setItem(key, value);
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch (error) {
    // Session storage can be unavailable in restricted browser contexts.
  }
}

let appInitialized = false;

function initializeApp() {
  if (appInitialized) return;
  appInitialized = true;
  cacheElements();
  bindEvents();
  applyStudioRoute({ loadRun: false, render: false });
  renderAll();
  void loadAll();
  setInterval(loadRunsAndJobs, 3000);
  setInterval(() => loadSelectedRunOperatorJobs({ silent: true }), 3000);
  setInterval(syncActiveAgentSession, 5000);
  setInterval(refreshPlatformStatus, 6000);
  setInterval(refreshInterfaceLaunchActivity, 1000);
  setInterval(refreshActiveStudyLaunchElapsed, 1000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initializeApp);
} else {
  initializeApp();
}

function cacheElements() {
  for (const id of [
    "healthStatus",
    "sidebarCodeServer",
    "sidebarServiceStatus",
    "studioSettingsButton",
    "pageTitle",
    "pageSubtitle",
    "refreshButton",
    "newSessionButton",
    "assistantToggleButton",
    "activeInterfaceBar",
    "activeInterfaceOpenButton",
    "activeInterfaceLabel",
    "activeInterfaceSubtitle",
    "activeInterfaceStopButton",
    "assistantBackButton",
    "assistantTitle",
    "assistantSubtitle",
    "assistantSessionList",
    "assistantSessionCards",
    "assistantContextHint",
    "assistantResizeHandle",
    "closeAssistantButton",
    "workspaceTitleInput",
    "workspaceCommitButton",
    "openWorkspaceExternalButton",
    "sessionCount",
    "sessionList",
    "newWorkspaceButton",
    "openLocalFolderButton",
    "sessionTitle",
    "sessionPath",
    "sessionStatus",
    "workspaceContextNotice",
    "sessionSummary",
    "sessionFiles",
    "sessionContext",
    "sessionTools",
    "sessionWorkspaceActions",
    "codeWorkbench",
    "previewWorkbench",
    "setupWorkbench",
    "workspaceSetupContent",
    "embeddedCodeWorkspace",
    "embeddedCodeWorkspaceEmpty",
    "embeddedCodeWorkspacePath",
    "codeWorkspaceEmptyTitle",
    "codeWorkspaceEmptyBody",
    "startEmbeddedCodeButton",
    "reloadEmbeddedCodeButton",
    "pauseCodeWorkspaceButton",
    "workspacePreviewFrame",
    "workspacePreviewEmpty",
    "workspacePreviewPort",
    "workspacePreviewStatus",
    "workspacePreviewTitle",
    "workspacePreviewBody",
    "workspaceInterfaceConflictActions",
    "returnToActiveInterfaceButton",
    "stopActiveInterfaceButton",
    "workspaceInterfaceLaunchStatus",
    "launchWorkspaceInterfaceButton",
    "openWorkspacePreviewButton",
    "reloadWorkspacePreviewButton",
    "agentTimeline",
    "agentInput",
    "sendAgentButton",
    "sessionBottom",
    "componentList",
    "catalogSources",
    "componentDetail",
    "planList",
    "planDetail",
    "totalRuns",
    "runningRuns",
    "completedTrials",
    "failureCount",
    "runFilter",
    "componentSearch",
    "componentPackageFilter",
    "planSearch",
    "runsTable",
    "runDetail",
    "selectionContentDrawerHost",
    "assistantLauncherSubtitle",
    "settingsModal",
    "settingsCloseButton",
    "settingsCancelButton",
    "settingsSaveButton",
    "openHandsEnabled",
    "openHandsBaseUrl",
    "openHandsSessionEndpoint",
    "openHandsModel",
    "openHandsApiKey",
    "openHandsClearApiKey",
    "openHandsStatus",
    "environmentVariablesList",
    "environmentVariableName",
    "environmentVariableValue",
    "environmentVariableAddButton",
    "assistantSkillsInput",
    "assistantMcpServersInput",
    "assistantMcpFilterRegex",
    "assistantCustomToolsInput",
    "assistantPermissionFileWrite",
    "assistantPermissionShellRun",
    "assistantPermissionCatalogRegistration",
    "assistantPermissionStudyLaunch",
    "assistantPermissionJobStop",
    "workspaceCleanupModal",
    "workspaceCleanupTitle",
    "workspaceCleanupBody",
    "workspaceCleanupKeepButton",
    "workspaceCleanupDeleteButton",
    "openLocalFolderModal",
    "openLocalFolderDialog",
    "openLocalFolderPath",
    "openLocalFolderName",
    "openLocalFolderError",
    "openLocalFolderCancelButton",
    "openLocalFolderSubmitButton",
    "registrationConfirmationModal",
    "registrationConfirmationCloseButton",
    "registrationConfirmationBody",
    "registrationConfirmationError",
    "registrationConfirmationCancelButton",
    "registrationConfirmationSubmitButton",
    "candidateTryModal",
    "candidateTryDialog",
    "candidateTryTitle",
    "candidateTryIntro",
    "candidateTryCloseButton",
    "candidateTryBody",
    "candidateTryActions",
    "candidateTryCancelButton",
    "candidateTrySubmitButton",
    "childRunConfirmationModal",
    "childRunConfirmationDialog",
    "childRunConfirmationTitle",
    "childRunConfirmationCloseButton",
    "childRunConfirmationBody",
    "childRunConfirmationCancelButton",
    "childRunConfirmationSubmitButton",
    "runStopModal",
    "runStopDialog",
    "runStopTitle",
    "runStopBody",
    "runStopError",
    "runStopCancelButton",
    "runStopSubmitButton",
    "interfaceStopModal",
    "interfaceStopDialog",
    "interfaceStopBody",
    "interfaceStopError",
    "interfaceStopCancelButton",
    "interfaceStopDiscardButton",
    "interfaceStopSaveButton",
  ]) {
    els[id] = document.getElementById(id);
  }
}

function bindEvents() {
  const on = (element, eventName, handler) => {
    if (element) element.addEventListener(eventName, handler);
  };
  window.optpilotStudioOpenSettings = openSettings;
  on(els.studioSettingsButton, "click", () => openSettings({ tab: "assistant" }));
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.settingsTab = button.dataset.settingsTab || "assistant";
      renderSettingsModal();
    });
  });
  document.querySelectorAll("[data-component-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.componentFilter = button.dataset.componentFilter;
      renderCatalog();
    });
  });
  document.querySelectorAll("[data-run-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.runStatusFilter = button.dataset.runFilter;
      renderRuns();
    });
  });
  document.querySelectorAll("[data-session-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.sessionTab = button.dataset.sessionTab;
      renderSessionBottom();
    });
  });
  document.querySelectorAll("[data-workbench-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.workbenchMode === "code") {
        setWorkbenchMode("code");
        openCodeServerEmbedded();
      } else if (button.dataset.workbenchMode === "setup") {
        openRegistrationMenu();
      } else {
        setWorkbenchMode(button.dataset.workbenchMode);
      }
    });
  });
  on(els.refreshButton, "click", loadAll);
  on(els.settingsCloseButton, "click", closeSettings);
  on(els.settingsCancelButton, "click", closeSettings);
  on(els.settingsSaveButton, "click", saveSettings);
  on(els.environmentVariableAddButton, "click", addEnvironmentVariableDraft);
  on(els.environmentVariablesList, "click", (event) => {
    const removeButton = event.target && event.target.closest && event.target.closest("[data-env-draft-remove]");
    if (!removeButton) return;
    state.environmentVariableDrafts = state.environmentVariableDrafts.filter((item) => item.name !== removeButton.dataset.envDraftRemove);
    renderEnvironmentVariablesList();
  });
  on(els.environmentVariableValue, "keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addEnvironmentVariableDraft();
    }
  });
  on(els.settingsModal, "click", (event) => {
    if (event.target === els.settingsModal) closeSettings();
  });
  on(els.workspaceCleanupKeepButton, "click", cancelPendingWorkspaceDelete);
  on(els.workspaceCleanupDeleteButton, "click", deletePendingWorkspaceDraft);
  on(els.workspaceCleanupModal, "click", (event) => {
    if (event.target === els.workspaceCleanupModal) cancelPendingWorkspaceDelete();
  });
  on(els.candidateTryCloseButton, "click", () => closeCandidateTrySheet());
  on(els.candidateTryCancelButton, "click", () => closeCandidateTrySheet());
  on(els.candidateTrySubmitButton, "click", confirmCandidateTry);
  on(els.candidateTryBody, "change", updateCandidateTrySheet);
  on(els.candidateTryModal, "click", (event) => {
    if (event.target === els.candidateTryModal) closeCandidateTrySheet();
  });
  on(els.candidateTryModal, "keydown", handleCandidateTrySheetKeydown);
  on(els.childRunConfirmationCloseButton, "click", closeChildRunConfirmation);
  on(els.childRunConfirmationCancelButton, "click", closeChildRunConfirmation);
  on(els.childRunConfirmationSubmitButton, "click", confirmChildRunCreation);
  on(els.childRunConfirmationModal, "click", (event) => {
    if (event.target === els.childRunConfirmationModal) closeChildRunConfirmation();
  });
  on(els.childRunConfirmationModal, "keydown", handleChildRunConfirmationKeydown);
  on(els.runStopCancelButton, "click", closeRunStopConfirmation);
  on(els.runStopSubmitButton, "click", confirmRunStop);
  on(els.runStopModal, "click", (event) => {
    if (event.target === els.runStopModal) closeRunStopConfirmation();
  });
  on(els.runStopModal, "keydown", handleRunStopConfirmationKeydown);
  on(els.interfaceStopCancelButton, "click", closeInterfaceStopConfirmation);
  on(els.interfaceStopDiscardButton, "click", discardPendingInterfaceOutputsAndStop);
  on(els.interfaceStopSaveButton, "click", savePendingInterfaceOutputAndContinueStop);
  on(els.interfaceStopModal, "click", (event) => {
    if (event.target === els.interfaceStopModal) closeInterfaceStopConfirmation();
  });
  on(els.interfaceStopModal, "keydown", handleInterfaceStopConfirmationKeydown);
  on(els.newSessionButton, "click", createAgentSession);
  on(els.newWorkspaceButton, "click", createBlankSession);
  on(els.openLocalFolderButton, "click", openLocalFolderDialog);
  on(els.openLocalFolderCancelButton, "click", closeLocalFolderDialog);
  on(els.openLocalFolderSubmitButton, "click", connectLocalFolder);
  on(els.openLocalFolderPath, "keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      connectLocalFolder();
    }
  });
  on(els.openLocalFolderModal, "click", (event) => {
    if (event.target === els.openLocalFolderModal) closeLocalFolderDialog();
  });
  on(els.openLocalFolderModal, "keydown", handleLocalFolderDialogKeydown);
  on(els.registrationConfirmationCloseButton, "click", closeRegistrationConfirmation);
  on(els.registrationConfirmationCancelButton, "click", closeRegistrationConfirmation);
  on(els.registrationConfirmationSubmitButton, "click", confirmCheckedRegistration);
  on(els.registrationConfirmationModal, "click", (event) => {
    if (event.target === els.registrationConfirmationModal) closeRegistrationConfirmation();
  });
  on(els.registrationConfirmationModal, "keydown", handleRegistrationConfirmationKeydown);
  on(els.assistantToggleButton, "click", toggleAssistant);
  on(els.activeInterfaceOpenButton, "click", openActiveInterfaceLocation);
  on(els.activeInterfaceStopButton, "click", stopActiveInterfaceFromGlobalControl);
  on(els.returnToActiveInterfaceButton, "click", openActiveInterfaceLocation);
  on(els.stopActiveInterfaceButton, "click", stopActiveInterfaceFromGlobalControl);
  on(els.assistantResizeHandle, "pointerdown", startAssistantResize);
  on(els.assistantResizeHandle, "mousedown", startAssistantResize);
  on(els.assistantBackButton, "click", () => {
    state.assistantMode = "sessions";
    renderAssistant();
  });
  on(els.closeAssistantButton, "click", () => {
    state.assistantMode = "chat";
    setAssistantOpen(false);
  });
  on(els.workspaceTitleInput, "keydown", handleWorkspaceTitleKeydown);
  on(els.workspaceTitleInput, "blur", saveWorkspaceTitleFromInput);
  on(els.workspaceCommitButton, "click", commitManagedWorkspace);
  on(els.openWorkspaceExternalButton, "click", openActiveWorkspaceExternal);
  on(els.startEmbeddedCodeButton, "click", startCodeWorkspaceFromUser);
  on(els.reloadEmbeddedCodeButton, "click", reloadEmbeddedCodeWorkspace);
  on(els.pauseCodeWorkspaceButton, "click", stopCodeServer);
  on(els.workspacePreviewPort, "input", updateWorkspacePreviewPort);
  on(els.launchWorkspaceInterfaceButton, "click", launchActiveWorkbenchInterface);
  on(els.openWorkspacePreviewButton, "click", openWorkspacePreview);
  on(els.reloadWorkspacePreviewButton, "click", reloadWorkspacePreview);
  on(els.sendAgentButton, "click", handleAgentActionButton);
  on(els.agentInput, "keydown", handleAgentInputKeydown);
  on(els.agentInput, "input", () => {
    els.agentInput.dataset.touched = els.agentInput.value ? "true" : "";
  });
  on(els.runFilter, "input", renderRuns);
  on(els.componentSearch, "input", () => {
    state.componentSearch = els.componentSearch.value;
    renderCatalog();
  });
  on(els.componentPackageFilter, "change", () => {
    state.componentPackageFilter = els.componentPackageFilter.value || "all";
    renderCatalog();
  });
  on(els.planSearch, "input", () => {
    state.planSearch = els.planSearch.value;
    renderExperiments();
  });
  window.addEventListener("keydown", handleSelectionContentKeydown);
  window.addEventListener("beforeunload", releaseSelectionContentViewOnUnload);
  window.addEventListener("hashchange", () => applyStudioRoute({ loadRun: true }));
}

async function loadAll() {
  await Promise.all([loadWorkspace(), loadRuntimeHealth(), loadCodeServerStatus(), loadAgentSettings(), loadCatalogAndCompatibility({ strict: false }), loadUiWorkspaces(), loadStudyDrafts(), loadAgentSessions(), loadRunsAndJobs()]);
  rebuildDerivedState();
  const routedRunId = applyStudioRoute({ loadRun: false, render: false });
  renderAll();
  if (routedRunId) {
    await loadRunDetail(routedRunId, { keepTab: true, skipListRender: true, fromRoute: true }).catch(() => {});
  }
  resumeStoredInterfaceLaunch();
  resumeStoredStudyLaunch();
}

async function refreshPlatformStatus() {
  await Promise.all([loadRuntimeHealth(), loadCodeServerStatus(), loadAgentSettings()]);
  renderPlatformStatus();
  renderOpenHandsStatus();
}

async function loadAgentSettings() {
  try {
    const payload = await getJson("/api/agent/settings");
    state.agentSettings = payload.settings || null;
    state.agentRuntimeStatus = payload.status || null;
  } catch (error) {
    state.agentSettings = null;
    state.agentRuntimeStatus = { runtime: "openhands", enabled: false, mode: "unavailable", error: String(error.message || error) };
  }
}

async function loadWorkspace() {
  try {
    state.workspace = await getJson("/api/workspace");
    if (state.workspace.code_server) state.codeServer = state.workspace.code_server;
    state.platformReady = true;
  } catch (error) {
    state.workspace = null;
    state.platformReady = false;
  }
}

async function loadRuntimeHealth() {
  try {
    state.runtime = await getJson("/api/runtime/health");
  } catch (error) {
    state.runtime = { error: String(error.message || error) };
  }
}

async function loadCodeServerStatus() {
  try {
    state.codeServer = await getJson("/api/code-server/status");
  } catch (error) {
    state.codeServer = { available: false, installed: false, running: false, error: String(error.message || error) };
  }
  updateSidebarCodeServerStatus();
}

async function loadCatalogAndCompatibility(options = {}) {
  const strict = options.strict !== false;
  const requestSeq = ++state.catalogRequestSeq;
  state.catalogLoading = true;
  state.catalogError = "";
  if (state.view === "catalog") renderCatalog();

  const settle = (promise) => promise.then(
    (value) => ({ status: "fulfilled", value }),
    (reason) => ({ status: "rejected", reason }),
  );
  const catalogResultPromise = settle(getJson("/api/catalog"));
  const compatibilityResultPromise = settle(getJson("/api/compatibility"));
  const catalogResult = await catalogResultPromise;

  if (requestSeq === state.catalogRequestSeq) {
    if (catalogResult.status === "fulfilled") {
      state.catalog = catalogResult.value;
      state.catalogLoaded = true;
      applyStudioRoute({ loadRun: false, render: false });
    } else {
      state.catalogError = boundedPublicActionError(
        catalogResult.reason,
        "Catalog data could not be loaded.",
      );
    }
    state.catalogLoading = false;
    if (state.view === "catalog") renderCatalog();
  }

  const compatibilityResult = await compatibilityResultPromise;
  if (requestSeq === state.catalogRequestSeq) {
    if (compatibilityResult.status === "fulfilled") {
      state.compatibility = compatibilityResult.value;
      state.compatibilityError = "";
    } else {
      state.compatibilityError = boundedPublicActionError(
        compatibilityResult.reason,
        "Compatibility data could not be loaded.",
      );
    }
  }

  const failure = catalogResult.status === "rejected"
    ? catalogResult.reason
    : compatibilityResult.status === "rejected"
    ? compatibilityResult.reason
    : null;
  if (strict && failure) throw failure;
  return {
    catalog: state.catalog,
    compatibility: state.compatibility,
    catalog_error: state.catalogError,
    compatibility_error: state.compatibilityError,
  };
}

async function loadUiWorkspaces() {
  try {
    const payload = await getJson("/api/workspaces");
    state.uiWorkspaces = payload.workspaces || [];
  } catch (error) {
    state.uiWorkspaces = [];
  }
}

async function loadStudyDrafts() {
  try {
    const payload = await getJson("/api/studies/drafts");
    state.studyDrafts = payload.drafts || [];
  } catch (error) {
    state.studyDrafts = [];
  }
}

async function loadAgentSessions() {
  try {
    const payload = await getJson("/api/agent-sessions");
    const sessions = payload.sessions || [];
    state.agentSessions = sessions.map((session) => ({
      id: session.id,
      title: session.title,
      description: session.description,
      status: session.status || "idle",
      effective_status: session.effective_status || session.status || "idle",
      pending_approval_count: Number(session.pending_approval_count || 0),
      active_approval_ids: session.active_approval_ids || [],
      queued_approval_count: Number(session.queued_approval_count || 0),
      createdAt: session.created_at || session.createdAt || "",
    }));
    state.agentWorkspaceAttachments = {};
    state.selectedWorkspaceByAgentSession = {};
    state.assistantMessagesBySession = {};
    state.agentApprovalsBySession = {};
    state.agentEventsBySession = {};
    sessions.forEach((session) => {
      state.agentWorkspaceAttachments[session.id] = session.attached_workspace_ids || [];
      state.selectedWorkspaceByAgentSession[session.id] = session.selected_workspace_id || null;
      state.assistantMessagesBySession[session.id] = (session.messages || []).map(agentMessageFromPayload);
      state.agentApprovalsBySession[session.id] = session.approvals || [];
      state.agentEventsBySession[session.id] = session.events || [];
    });
    ensureSelectedAgentSession();
  } catch (error) {
    state.agentSessions = [];
    state.agentApprovalsBySession = {};
    state.agentEventsBySession = {};
    ensureSelectedAgentSession();
  }
}

async function loadRunsAndJobs() {
  if (state.runsRefreshInFlight) return;
  state.runsRefreshInFlight = true;
  let runsPayload;
  try {
    runsPayload = await getJson("/api/runs");
  } catch (error) {
    state.runsRefreshInFlight = false;
    return;
  }
  try {
    state.runCatalog = runsPayload.catalog || null;
    state.runUnavailable = runsPayload.unavailable || null;
    const catalogByRunId = new Map(
      (state.runCatalog && state.runCatalog.items || []).map((item) => [item.run_id, item]),
    );
    state.runs = (runsPayload.runs || []).map((run) => {
      const runId = canonicalRunId(run);
      const catalogEntry = catalogByRunId.get(runId);
      return catalogEntry
        ? {
            ...run,
            head: run.head ?? catalogEntry.head,
            budget: run.budget ?? catalogEntry.budget,
            updated_at: run.updated_at ?? catalogEntry.updated_at,
          }
        : run;
    });
    if (!state.selectedRunId && state.runs[0]) state.selectedRunId = canonicalRunId(state.runs[0]);
    const refreshDetail = shouldRefreshSelectedRunDetail();
    if (state.view === "runs") renderRuns();
    if (refreshDetail) {
      try {
        await loadRunDetail(state.selectedRunId, { keepTab: true, skipListRender: true });
      } catch (error) {
        // A live head may advance during refresh; the next poll will retry coherently.
      }
    }
  } finally {
    state.runsRefreshInFlight = false;
  }
}

function shouldRefreshSelectedRunDetail() {
  if (!state.selectedRunId || state.view !== "runs") return false;
  // Poll the Run list while someone is editing the Shortlist, but do not
  // replace the detail DOM underneath their caret.  A manual reload or the
  // next poll after blur/save will reconcile the current Run head.
  if (shortlistEditingInProgress()) return false;
  if (!state.selectedRun || !state.selectedRun.run) return true;
  if (canonicalRunId(state.selectedRun.run) !== state.selectedRunId) return true;
  const summary = state.runs.find((run) => canonicalRunId(run) === state.selectedRunId);
  if (!summary) return false;
  return runSummaryChanged(summary, state.selectedRun.run, state.selectedRun);
}

function shortlistEditingInProgress() {
  if (state.activeRunTab !== "review") return false;
  const draft = reviewDraft();
  if (draft && draft.dirty) return true;
  const active = document.activeElement;
  return Boolean(
    active
    && els.runDetail
    && els.runDetail.contains(active)
    && active.matches("[data-review-title], [data-review-note]"),
  );
}

function runSummaryChanged(summary, detailRun, detail) {
  const listHead = runHead(summary);
  const detailHead = detail && detail.workbench && detail.workbench.head || runHead(detailRun);
  if (listHead && detailHead) {
    return listHead.revision !== detailHead.revision || listHead.sequence !== detailHead.sequence;
  }
  const fields = [
    "status",
    "run_status",
    "stop_code",
    "accepted_trials",
    "terminal_trials",
    "attempt_count",
    "observation_count",
    "failure_count",
    "final_failure_count",
    "updated_at",
  ];
  if (fields.some((field) => String(summary[field] ?? "") !== String(detailRun[field] ?? ""))) return true;
  const listBest = runBestPrimaryValue(summary);
  const detailBest = runBestPrimaryValue(detailRun);
  if (
    listBest.available !== detailBest.available
    || listBest.reason !== detailBest.reason
    || listBest.candidateId !== detailBest.candidateId
    || String(listBest.value ?? "") !== String(detailBest.value ?? "")
  ) return true;
  return String(summary.updated_at ?? "") !== String(detailRun.updated_at ?? "");
}

function rebuildDerivedState() {
  const previousSessionId = state.selectedSessionId;
  const previousPlanId = state.selectedPlanId;
  state.sessions = buildSessions();
  state.plans = buildPlans();
  ensureAgentSessions();
  state.selectedSessionId = state.sessions.some((session) => session.id === previousSessionId)
    ? previousSessionId
    : state.sessions[0] && state.sessions[0].id || null;
  const session = currentSession();
  state.selectedFileKey = session && session.files[state.selectedFileKey] ? state.selectedFileKey : firstFileKey(session);
  state.selectedPlanId = state.plans.some((plan) => plan.id === previousPlanId)
    ? previousPlanId
    : state.plans[0] && state.plans[0].id;
  if (!state.selectedComponentKey) {
    const firstComponent = allComponents()[0];
    state.selectedComponentKey = firstComponent && firstComponent.key;
  }
}

function ensureAgentSessions() {
  const workspaceIds = state.sessions.map((session) => session.id);
  if (!state.agentSessions.length) {
    const session = {
      id: "agent-session-main",
      title: "Main conversation",
      description: "General OptPilot work",
      createdAt: "now",
    };
    state.agentSessions = [session];
    setSelectedAgentSessionState(session.id);
    state.agentWorkspaceAttachments[session.id] = [];
    state.selectedWorkspaceByAgentSession[session.id] = null;
    state.assistantMessagesBySession[session.id] = defaultAssistantMessages();
    state.agentEventsBySession[session.id] = [];
    return;
  }
  const known = new Set(workspaceIds);
  state.agentSessions.forEach((session) => {
    const attached = state.agentWorkspaceAttachments[session.id] || [];
    state.agentWorkspaceAttachments[session.id] = attached.filter((id) => known.has(id));
    if (!state.assistantMessagesBySession[session.id]) {
      state.assistantMessagesBySession[session.id] = defaultAssistantMessages();
    }
    if (!state.agentEventsBySession[session.id]) {
      state.agentEventsBySession[session.id] = [];
    }
  });
  ensureSelectedAgentSession();
}

function ensureSelectedAgentSession() {
  if (state.agentSessions.some((session) => session.id === state.selectedAgentSessionId)) {
    storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
    return;
  }
  const withWorkspaces = state.agentSessions.find((session) => (state.agentWorkspaceAttachments[session.id] || []).length);
  setSelectedAgentSessionState(
    (withWorkspaces || state.agentSessions[0] || {}).id || null,
  );
}

function defaultAssistantMessages() {
  return [["assistant", "Ready", "I can use the current page, Catalog, Studies, Runs, and the files in Workspaces you explicitly make available to this conversation.", {
    id: "default-ready",
    createdAt: new Date().toISOString(),
    source: "studio_system",
    memoryScope: "ui_history",
  }]];
}

function agentMessageFromPayload(message) {
  const source = messageSourceFromPayload(message || {});
  return [
    message.role === "assistant" ? "assistant" : message.role || "user",
    message.title || "",
    message.content || "",
    {
      id: message.id || "",
      title: message.title || "",
      createdAt: message.created_at || message.createdAt || "",
      source,
      memoryScope: message.memory_scope || message.memoryScope || defaultMessageMemoryScope(message.role || "user", source),
      persisted: true,
    },
  ];
}

function messageSourceFromPayload(message) {
  if (message.source) return message.source;
  const role = message.role || "user";
  if (role === "user") return "user";
  const title = message.title || "";
  const dispatch = message.dispatch && typeof message.dispatch === "object" ? message.dispatch : {};
  if (role === "assistant" && (title === "OpenHands" || dispatch.conversation_id)) return "openhands";
  if (role === "assistant" && title === "Assistant" && dispatch.transport) return "model_chat";
  return defaultMessageSource(role);
}

function currentAgentSession() {
  return state.agentSessions.find((session) => session.id === state.selectedAgentSessionId) || state.agentSessions[0] || null;
}

function assistantSessionStatus(session = currentAgentSession()) {
  return session && (session.effective_status || session.status) || "";
}

function currentAssistantMessages() {
  const session = currentAgentSession();
  if (!session) return defaultAssistantMessages();
  if (!state.assistantMessagesBySession[session.id]) state.assistantMessagesBySession[session.id] = defaultAssistantMessages();
  return state.assistantMessagesBySession[session.id];
}

function currentAssistantApprovals() {
  const session = currentAgentSession();
  if (!session) return [];
  const activeIds = new Set((session.active_approval_ids || []).map(String));
  const seen = new Set();
  return (state.agentApprovalsBySession[session.id] || []).filter((approval) => {
    if (approval.status !== "pending") return false;
    if (activeIds.size && !activeIds.has(String(approval.id))) return false;
    const key = approvalDisplayKey(approval);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function pendingApprovalKeyForSession(sessionId) {
  if (!sessionId) return "";
  const seen = new Set();
  const keys = [];
  (state.agentApprovalsBySession[sessionId] || []).forEach((approval) => {
    if (!approval || approval.status !== "pending") return;
    const key = approvalDisplayKey(approval);
    if (!key || seen.has(key)) return;
    seen.add(key);
    keys.push(key);
  });
  return keys.sort().join("\n");
}

function approvalDisplayKey(approval) {
  if (!approval || typeof approval !== "object") return "";
  const displayPayload = approval.display_payload && typeof approval.display_payload === "object" ? approval.display_payload : {};
  const args = displayPayload.redacted_arguments && typeof displayPayload.redacted_arguments === "object"
    ? { ...displayPayload.redacted_arguments }
    : approval.arguments && typeof approval.arguments === "object" ? { ...approval.arguments } : {};
  delete args._openhands_tool_call_id;
  delete args.approved;
  delete args.description;
  return stableJsonStringify({
    tool: approval.tool || "",
    kind: approval.kind || "",
    arguments: args,
  });
}

function stableJsonStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableJsonStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJsonStringify(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function currentAssistantEvents() {
  const session = currentAgentSession();
  if (!session) return [];
  return state.agentEventsBySession[session.id] || [];
}

async function syncActiveAgentSession() {
  if (!state.assistantOpen) return;
  const session = currentAgentSession();
  if (!session || session.id.startsWith("agent-session-")) return;
  if (!["waiting_for_agent", "running"].includes(assistantSessionStatus(session))) return;
  if (state.syncingAgentSessionIds.has(session.id)) return;
  state.syncingAgentSessionIds.add(session.id);
  try {
    const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(session.id)}/sync`, {});
    if (payload.session) {
      await updateAgentSessionFromPayload(payload.session);
    }
  } catch (error) {
    // Keep the transcript stable; the next poll or refresh can retry.
  } finally {
    state.syncingAgentSessionIds.delete(session.id);
  }
}

function pushAssistantMessage(message, options = {}) {
  const session = currentAgentSession();
  if (!session) return;
  if (!state.assistantMessagesBySession[session.id]) state.assistantMessagesBySession[session.id] = defaultAssistantMessages();
  const localMessage = localAssistantMessage(message);
  state.assistantMessagesBySession[session.id].push(localMessage);
  if (options.persist !== false && shouldPersistLocalAssistantMessage(localMessage, session)) {
    persistAssistantMessage(localMessage, { keepalive: true, refreshSession: false, sessionId: session.id });
  }
}

function localAssistantMessage(message) {
  if (message && message[3] && message[3].createdAt) return message;
  const role = message && message[0] || "assistant";
  const metadata = message && message[3] && typeof message[3] === "object" ? message[3] : {};
  return [
    role,
    message && message[1] || "",
    message && message[2] || "",
    {
      id: `local-${Date.now().toString(36)}`,
      createdAt: new Date().toISOString(),
      source: metadata.source || defaultMessageSource(role),
      memoryScope: metadata.memoryScope || metadata.memory_scope || defaultMessageMemoryScope(role, metadata.source || ""),
    },
  ];
}

function defaultMessageSource(role) {
  return role === "user" ? "user" : "studio_ui";
}

function defaultMessageMemoryScope(role, source = "") {
  if (role === "user" || source === "openhands") return "openhands_conversation";
  return "ui_history";
}

function shouldPersistLocalAssistantMessage(message, session) {
  if (!session || !session.id || session.id.startsWith("agent-session-")) return false;
  const role = message && message[0] || "";
  const content = String(message && message[2] || "").trim();
  const metadata = message && message[3] || {};
  if (!content || metadata.persisted) return false;
  return role !== "user";
}

function assistantVisibleContext() {
  const workspace = currentSession();
  const workspacePreview = workspace ? currentWorkspacePreview(workspace) : null;
  const isCatalogPage = state.view === "catalog";
  const isStudiesPage = state.view === "experiments";
  const isRunsPage = state.view === "runs";
  const isEditorPage = state.view === "workspace";
  const isRegistrationMode = state.assistantMode === "registration";
  const component = componentByKey(state.selectedComponentKey);
  const plan = currentPlan();
  const selectedRun = state.selectedRun && state.selectedRun.run
    ? state.selectedRun.run
    : state.runs.find((run) => canonicalRunId(run) === state.selectedRunId);
  return {
    current_page: state.view,
    assistant_mode: state.assistantMode,
    selected_workspace: isEditorPage && workspace ? {
      id: workspace.backendWorkspaceId || workspace.id,
      title: workspace.title,
      root: workspace.codeFolder || workspace.path,
      mode: workspace.mode,
      kind: workspace.kind,
      registered_entries: workspace.registeredEntries || [],
    } : null,
    selected_catalog_entry: isCatalogPage && component ? {
      kind: component.kind,
      id: component.entry.id,
      label: component.entry.label,
      ref: component.entry.ref || null,
    } : null,
    selected_study_plan: isStudiesPage && plan ? {
      id: plan.id,
      title: plan.title,
      source: plan.source,
      status: plan.status,
      study_ref: plan.study && plan.study.ref || null,
      workspace_id: plan.draft && plan.draft.workspace_id || "",
      study_relative_path: plan.draft && plan.draft.study_relative_path || "",
      environment_ref: exactCatalogEntryRef(plan.environment),
      method_ref: exactCatalogEntryRef(plan.method),
      environment_id: plan.environment && plan.environment.id || "",
      method_id: plan.method && plan.method.id || "",
    } : null,
    selected_run: isRunsPage && selectedRun
      ? assistantSelectedRunContext(selectedRun)
      : null,
    registration_menu: isRegistrationMode && state.registrationDraft ? {
      workspace_id: state.registrationDraft.backendWorkspaceId || state.registrationDraft.workspaceId,
      status: state.registrationDraft.status,
      package_plan_id: state.registrationDraft.packagePlanId || "",
      classification: state.registrationDraft.classification || "",
      readiness: state.registrationDraft.readiness || "",
      package_plan: state.registrationDraft.packagePlan ? packagePlanContextSummary(state.registrationDraft.packagePlan) : null,
      warnings: state.registrationDraft.packagePlan && state.registrationDraft.packagePlan.warnings || [],
      selected_configs: (state.registrationDraft.configs || [])
        .filter((config) => config.selected)
        .map((config) => ({ path: config.backendPath || config.label, kind: config.kind, validation: config.validation })),
    } : null,
    code_editor: isEditorPage ? {
      embedded_url: state.embeddedCodeUrl,
      folder: state.embeddedCodeFolder,
      status: state.codeWorkspaceStatus,
    } : null,
    workspace_preview: isEditorPage && workspace ? {
      workspace_id: workspace.backendWorkspaceId || workspace.id,
      port: workspacePreview && workspacePreview.port || 5173,
      url: workspacePreview && workspacePreview.url || "",
      status: workspacePreview && workspacePreview.status || "idle",
      message: workspacePreview && workspacePreview.message || "",
      active: state.workbenchMode === "preview",
    } : null,
    assistant_runtime: state.agentRuntimeStatus || null,
  };
}

function assistantSelectedRunContext(selectedRun) {
  const runId = canonicalRunId(selectedRun);
  const selected = state.assistantRunSelection;
  const session = currentAgentSession();
  const context = { run_id: runId };
  if (
    selected && selected.run_id === runId && selected.handle
    && session && selected.session_id === session.id
  ) {
    context.selection_handle = selected.handle;
  }
  return context;
}

async function persistAssistantMessage(message, options = {}) {
  const session = options.sessionId
    ? state.agentSessions.find((item) => item.id === options.sessionId)
    : currentAgentSession();
  if (!session || !session.id || session.id.startsWith("agent-session-")) return null;
  const [role, title, content] = message;
  const metadata = message && message[3] || {};
  try {
    const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(session.id)}/message`, {
      role: role === "agent" ? "assistant" : role,
      title,
      content,
      source: metadata.source || defaultMessageSource(role),
      memory_scope: metadata.memoryScope || metadata.memory_scope || defaultMessageMemoryScope(role, metadata.source || ""),
      ui_context: assistantVisibleContext(),
    }, { keepalive: Boolean(options.keepalive) });
    if (payload.session && options.refreshSession !== false) await updateAgentSessionFromPayload(payload.session);
    return payload;
  } catch (error) {
    if (options.rethrowError) throw error;
    // Keep the local transcript usable if the backend is unavailable.
    return null;
  }
}

function mergeAgentSessionPayload(session) {
  if (!session || !session.id) return false;
  const existing = state.agentSessions.find((item) => item.id === session.id);
  const previousAttachments = state.agentWorkspaceAttachments[session.id] || [];
  const nextAttachments = session.attached_workspace_ids || [];
  const workspacesChanged = !sameStringList(previousAttachments, nextAttachments);
  const summary = {
    id: session.id,
    title: session.title,
    description: session.description,
    status: session.status || "idle",
    effective_status: session.effective_status || session.status || "idle",
    pending_approval_count: Number(session.pending_approval_count || 0),
    active_approval_ids: session.active_approval_ids || [],
    queued_approval_count: Number(session.queued_approval_count || 0),
    createdAt: session.created_at || "",
  };
  state.agentSessions = existing
    ? state.agentSessions.map((item) => item.id === session.id ? { ...item, ...summary } : item)
    : [summary, ...state.agentSessions];
  state.agentWorkspaceAttachments[session.id] = nextAttachments;
  state.selectedWorkspaceByAgentSession[session.id] = session.selected_workspace_id || null;
  state.agentApprovalsBySession[session.id] = session.approvals || state.agentApprovalsBySession[session.id] || [];
  state.agentEventsBySession[session.id] = session.events || state.agentEventsBySession[session.id] || [];
  if (session.messages) {
    state.assistantMessagesBySession[session.id] = session.messages.map(agentMessageFromPayload);
  }
  return workspacesChanged;
}

function adoptWorkspacePreviewToolResults(session, options = {}) {
  if (!session || !Array.isArray(session.events)) return false;
  let activated = false;
  session.events.forEach((event) => {
    if (!event || event.type !== "optpilot_tool_result" || !event.id) return;
    if (state.handledPreviewEventIds.has(event.id)) return;
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    if (payload.tool !== "optpilot_workspace_preview_open" || payload.ok === false) return;
    const result = parseJsonPreview(payload.result_preview);
    const data = result && result.data && typeof result.data === "object" ? result.data : {};
    if (!data.preview_url) return;
    state.handledPreviewEventIds.add(event.id);
    const workspaceId = String(data.workspace_id || data.workspace && data.workspace.id || "");
    const sessionWorkspace = state.sessions.find((item) => item.id === workspaceId || item.backendWorkspaceId === workspaceId);
    if (!sessionWorkspace) return;
    const preview = currentWorkspacePreview(sessionWorkspace);
    preview.port = Number(data.port || preview.port || 5173);
    preview.url = String(data.preview_url || "");
    preview.status = "ready";
    preview.message = `Previewing port ${preview.port} through ${sessionWorkspace.title}.`;
    if (data.code_server && typeof data.code_server === "object") {
      state.codeServer = data.code_server;
      if (data.code_server.open_url) {
        state.embeddedCodeUrl = data.code_server.open_url;
        state.embeddedCodeFolder = data.folder || sessionWorkspace.codeFolder || "";
        state.codeWorkspaceStatus = "ready";
        state.codeWorkspaceMessage = "";
      }
    }
    if (options.activate) {
      setSelectedWorkspace(sessionWorkspace.id);
      state.workbenchMode = "preview";
      activated = true;
    }
  });
  return activated;
}

function parseJsonPreview(value) {
  if (!value || typeof value !== "string") return null;
  try {
    return JSON.parse(value);
  } catch (error) {
    return null;
  }
}

async function updateAgentSessionFromPayload(session) {
  const workspacesChanged = mergeAgentSessionPayload(session);
  if (workspacesChanged) {
    await refreshAgentWorkspaceState();
  }
  const previewActivated = adoptWorkspacePreviewToolResults(session, {
    activate: ["waiting_for_agent", "running"].includes(assistantSessionStatus(session)),
  });
  if (previewActivated) {
    if (state.view !== "workspace") {
      state.view = "workspace";
      renderNavigation();
    }
    renderWorkspace();
    renderAssistant();
    return;
  }
  if (!workspacesChanged) renderAssistant();
}

function sameStringList(left, right) {
  const a = (left || []).map(String);
  const b = (right || []).map(String);
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

async function refreshAgentWorkspaceState() {
  await Promise.all([loadUiWorkspaces(), loadCatalogAndCompatibility()]);
  rebuildDerivedState();
  if (state.view === "catalog") renderCatalog();
  renderWorkspace();
  renderAssistant();
}

function attachedWorkspaceIds(agentSessionId = state.selectedAgentSessionId) {
  const ids = state.agentWorkspaceAttachments[agentSessionId] || [];
  const known = new Set(state.sessions.map((session) => session.id));
  return ids.filter((id) => known.has(id));
}

function attachedWorkspaces() {
  const attached = new Set(attachedWorkspaceIds());
  return state.sessions.filter((session) => attached.has(session.id));
}

function orderedWorkspaceSessions() {
  const attached = new Set(attachedWorkspaceIds());
  return state.sessions
    .filter((session) => session.visibleInWorkspaces !== false)
    .map((session) => ({ ...session, attachedToCurrent: attached.has(session.id) }))
    .sort((left, right) => workspaceSortMs(right.updatedAt || right.createdAt) - workspaceSortMs(left.updatedAt || left.createdAt));
}

function isCatalogSourceView(session = currentSession()) {
  return Boolean(session && session.visibleInWorkspaces === false && session.sourceType === "catalog");
}

function rememberCatalogSourceComponent(component) {
  const key = String(component && component.key || componentLaunchKey(component));
  const kind = String(component && component.kind || "");
  const uid = String(component && component.entry && component.entry.uid || "");
  if (!key || !kind || !uid || key !== `${kind}:${uid}`) return null;
  state.catalogSourceComponents[key] = component;
  return component;
}

function catalogSourceComponentByKey(key) {
  const exactKey = String(key || "");
  if (!exactKey) return null;
  return componentByKey(exactKey) || state.catalogSourceComponents[exactKey] || null;
}

function launchInterfaceSummary(launch) {
  const profile = launch && launch.result && launch.result.interface;
  if (!profile || typeof profile !== "object" || Array.isArray(profile)) return {};
  return {
    profiles: [profile],
    defaultProfileId: String(launch.profile_id || profile.id || ""),
  };
}

function catalogComponentForActiveLaunch(launch = state.interfaceLaunch, session = null) {
  if (!launch || launch.launch_scope === "workspace-transient") return null;
  const launchKey = String(launch.key || "");
  const retained = catalogSourceComponentByKey(launchKey);
  if (retained) return retained;
  const kind = String(launch.kind || "");
  const uid = String(launch.uid || "");
  if (!new Set(["environment", "method", "resource"]).has(kind) || !uid) return null;
  if (launchKey !== `${kind}:${uid}`) return null;
  if (session && String(session.catalogComponentKey || "") !== launchKey) return null;
  const sessionInterface = session && session.interface && typeof session.interface === "object"
    ? session.interface
    : null;
  return rememberCatalogSourceComponent({
    key: launchKey,
    kind,
    entry: {
      uid,
      label: String(launch.label || "Catalog interface"),
      interface: sessionInterface || launchInterfaceSummary(launch),
    },
  });
}

function catalogSourceComponent(session = currentSession()) {
  if (!isCatalogSourceView(session)) return null;
  const preferredKey = String(session.catalogComponentKey || "");
  if (!preferredKey) return null;
  const preferred = catalogSourceComponentByKey(preferredKey)
    || catalogComponentForActiveLaunch(state.interfaceLaunch, session);
  const originKind = String(session.catalogOrigin && session.catalogOrigin.component_kind || "");
  if (preferred && (!originKind || preferred.kind === originKind)) return preferred;
  return null;
}

function currentCatalogInterfaceLaunch(session = currentSession()) {
  const launch = state.interfaceLaunch;
  if (!isCatalogSourceView(session) || !launch || launch.launch_scope === "workspace-transient") return null;
  const sessionLaunchId = String(session.catalogLaunchId || "");
  const activeLaunchId = String(launch.launch_id || "");
  if (sessionLaunchId && activeLaunchId) return sessionLaunchId === activeLaunchId ? launch : null;
  const sourceKey = String(session.catalogComponentKey || "");
  return sourceKey && sourceKey === String(launch.key || "") ? launch : null;
}

function catalogInterfacePreviewUrl(session = currentSession()) {
  const launch = currentCatalogInterfaceLaunch(session);
  if (!launch || launch.status !== "ready") return "";
  return String(launch.result && launch.result.preview && launch.result.preview.preview_url || "");
}

function isActiveInterfaceLaunch(launch = state.interfaceLaunch) {
  return Boolean(
    launch
    && !["failed", "stopped"].includes(String(launch.status || "")),
  );
}

function isViewingActiveInterface(launch = state.interfaceLaunch, session = currentSession()) {
  if (
    !isActiveInterfaceLaunch(launch)
    || state.view !== "workspace"
    || state.workbenchMode !== "preview"
    || !session
  ) return false;
  return launch.launch_scope === "workspace-transient"
    ? currentWorkspaceInterfaceLaunch(session) === launch
    : currentCatalogInterfaceLaunch(session) === launch;
}

function activeInterfaceSource(launch = state.interfaceLaunch) {
  if (!launch) return "";
  if (launch.launch_scope === "workspace-transient") {
    const session = workspaceSessionByBackendId(launch.source_workspace_id);
    return session && session.title || "Workspace";
  }
  return catalogSourceComponentByKey(launch.key) ? "Catalog item" : "Catalog interface";
}

function activeInterfaceStatusText(launch = state.interfaceLaunch, viewing = false) {
  const status = String(launch && launch.status || "");
  if (!status) return "Reconnecting…";
  if (["queued", "running"].includes(status)) return viewing ? "Starting here" : "Starting · Click to return";
  if (status === "stopping") return viewing ? "Stopping here" : "Stopping";
  if (status === "cleanup_pending") return viewing ? "Cleanup needed here" : "Cleanup needed · Click to return";
  return viewing ? "Viewing now" : "Running · Click to return";
}

function resetActiveInterfaceReturnState() {
  state.interfaceReturnPending = false;
  state.interfaceReturnError = "";
  state.interfaceReturnFallbackUrl = "";
}

function renderActiveInterfaceIndicator() {
  if (!els.activeInterfaceBar) return;
  const launch = state.interfaceLaunch;
  const active = isActiveInterfaceLaunch(launch);
  els.activeInterfaceBar.hidden = !active;
  if (!active) {
    els.activeInterfaceBar.classList.remove("is-current");
    return;
  }
  const viewing = isViewingActiveInterface(launch);
  const label = String(launch.label || "Interface");
  const source = activeInterfaceSource(launch);
  const status = activeInterfaceStatusText(launch, viewing);
  const returnError = String(state.interfaceReturnError || "");
  const returnPending = Boolean(state.interfaceReturnPending);
  const fallback = Boolean(state.interfaceReturnFallbackUrl);
  els.activeInterfaceBar.classList.toggle("is-current", viewing);
  els.activeInterfaceBar.setAttribute(
    "aria-label",
    viewing ? "Currently displayed running interface" : "Running interface available",
  );
  if (els.activeInterfaceLabel) els.activeInterfaceLabel.textContent = label;
  if (els.activeInterfaceSubtitle) {
    els.activeInterfaceSubtitle.textContent = returnPending
      ? "Opening its exact source…"
      : returnError || (source ? `${source} · ${status}` : status);
    els.activeInterfaceSubtitle.title = returnError || "";
  }
  if (els.activeInterfaceOpenButton) {
    els.activeInterfaceOpenButton.disabled = returnPending;
    els.activeInterfaceOpenButton.setAttribute("aria-busy", returnPending ? "true" : "false");
    if (viewing) els.activeInterfaceOpenButton.setAttribute("aria-current", "page");
    else els.activeInterfaceOpenButton.removeAttribute("aria-current");
    els.activeInterfaceOpenButton.title = fallback
      ? "Open the running interface in a new window"
      : viewing
      ? `${label} is open here`
      : `Return to ${label}`;
    els.activeInterfaceOpenButton.setAttribute(
      "aria-label",
      fallback
        ? `Open running interface in a new window: ${label}`
        : viewing
        ? `Running interface currently displayed: ${label}`
        : `Return to running interface: ${label}`,
    );
  }
  if (els.activeInterfaceStopButton) {
    const stopping = String(launch.status || "") === "stopping";
    els.activeInterfaceStopButton.disabled = stopping || !launch.launch_id;
    els.activeInterfaceStopButton.textContent = stopping
      ? "Stopping…"
      : String(launch.status || "") === "cleanup_pending"
      ? "Retry cleanup"
      : "Stop";
    els.activeInterfaceStopButton.title = `Stop ${label}`;
  }
}

function renderInterfaceConflictActions(otherLaunch) {
  if (!els.workspaceInterfaceConflictActions) return;
  const active = isActiveInterfaceLaunch(otherLaunch);
  els.workspaceInterfaceConflictActions.hidden = !active;
  if (!active) return;
  const label = String(otherLaunch.label || "running interface");
  const returnPending = Boolean(state.interfaceReturnPending);
  const fallback = Boolean(state.interfaceReturnFallbackUrl);
  if (els.returnToActiveInterfaceButton) {
    els.returnToActiveInterfaceButton.disabled = returnPending;
    els.returnToActiveInterfaceButton.setAttribute("aria-busy", returnPending ? "true" : "false");
    els.returnToActiveInterfaceButton.textContent = returnPending
      ? "Opening…"
      : fallback
      ? "Open running interface"
      : `Return to ${label}`;
  }
  if (els.stopActiveInterfaceButton) {
    const stopping = String(otherLaunch.status || "") === "stopping";
    els.stopActiveInterfaceButton.disabled = stopping || !otherLaunch.launch_id;
    els.stopActiveInterfaceButton.textContent = stopping
      ? "Stopping…"
      : String(otherLaunch.status || "") === "cleanup_pending"
      ? "Retry cleanup"
      : `Stop ${label}`;
  }
}

async function openActiveInterfaceLocation() {
  const launch = state.interfaceLaunch;
  if (!isActiveInterfaceLaunch(launch) || state.interfaceReturnPending) return;
  const launchId = String(launch.launch_id || "");
  const launchKey = String(launch.key || "");
  const launchStartedAt = Number(launch.startedAt || 0);
  const fallbackUrl = String(state.interfaceReturnFallbackUrl || "");
  if (fallbackUrl) {
    const externalWindow = reserveExternalWindow();
    if (!externalWindow) {
      state.interfaceReturnError = "The browser blocked the new window. Allow pop-ups for Studio, then try again.";
    } else if (!navigateExternalWindow(externalWindow, fallbackUrl)) {
      try { externalWindow.close(); } catch (_error) { /* best effort */ }
      state.interfaceReturnError = "Studio could not open the running interface in a new window. Try again or stop it.";
    } else {
      state.interfaceReturnError = "The original source view is unavailable, so the running interface opened in a separate window.";
    }
    renderActiveInterfaceReturnState();
    return;
  }

  state.interfaceReturnPending = true;
  state.interfaceReturnError = "";
  renderActiveInterfaceReturnState();
  try {
    state.workbenchMode = "preview";
    if (launch.launch_scope === "workspace-transient") {
      const workspaceId = String(launch.source_workspace_id || "");
      let session = workspaceSessionByBackendId(workspaceId);
      if (!session && workspaceId) {
        const payload = await getJson(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
        if (payload && payload.workspace) session = mergeUiWorkspace(payload.workspace);
      }
      if (!session) throw new Error("The source Workspace could not be found.");
      if (!activeInterfaceReturnStillCurrent(launchKey, launchId, launchStartedAt)) return;
      await selectSession(session.id);
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      state.interfaceReturnError = "";
      return;
    }

    let session = catalogSourceSessionByKey(launch.key);
    const component = catalogComponentForActiveLaunch(launch, session);
    if (!component) throw new Error("The exact Catalog source identity is unavailable.");
    if (session) {
      session.catalogLaunchId = launchId;
      rememberCatalogSourceComponent({
        ...component,
        entry: { ...component.entry, interface: session.interface || component.entry.interface },
      });
      showCatalogSourceSession(session, "preview");
    } else {
      session = await openComponentSession(component, "inspect", { workbenchMode: "preview" });
      if (!session) throw new Error("The exact Catalog source view could not be reopened.");
      if (!activeInterfaceReturnStillCurrent(launchKey, launchId, launchStartedAt)) return;
      session.catalogLaunchId = launchId;
      rememberCatalogSourceComponent({
        ...component,
        entry: { ...component.entry, interface: session.interface || component.entry.interface },
      });
      renderWorkspace();
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    state.interfaceReturnError = "";
  } catch (error) {
    if (!activeInterfaceReturnStillCurrent(launchKey, launchId, launchStartedAt)) return;
    const previewUrl = String(
      launch.status === "ready" && launch.result && launch.result.preview && launch.result.preview.preview_url || "",
    );
    state.interfaceReturnFallbackUrl = previewUrl;
    state.interfaceReturnError = previewUrl
      ? "The exact source view could not be reopened. The interface is still running; open it in a new window or stop it."
      : boundedPublicActionError(
        error,
        launch.launch_scope === "workspace-transient"
          ? "The source Workspace could not be reopened. Try again or stop the running interface."
          : "The exact Catalog version could not be reopened. Try again or stop the running interface.",
      );
  } finally {
    if (activeInterfaceReturnStillCurrent(launchKey, launchId, launchStartedAt)) {
      state.interfaceReturnPending = false;
      renderActiveInterfaceReturnState();
    }
  }
}

function activeInterfaceReturnStillCurrent(launchKey, launchId, launchStartedAt) {
  const current = state.interfaceLaunch;
  if (!isActiveInterfaceLaunch(current) || String(current.key || "") !== String(launchKey || "")) return false;
  const currentId = String(current.launch_id || "");
  if (launchId && currentId && currentId !== launchId) return false;
  const currentStartedAt = Number(current.startedAt || 0);
  if (!launchId && launchStartedAt && currentStartedAt && currentStartedAt !== launchStartedAt) return false;
  return true;
}

function renderActiveInterfaceReturnState() {
  renderActiveInterfaceIndicator();
  if (state.view === "workspace") renderPreviewWorkbench();
}

async function stopActiveInterfaceFromGlobalControl() {
  const launch = state.interfaceLaunch;
  if (!isActiveInterfaceLaunch(launch) || !launch.launch_id) return;
  await stopInterfaceLaunch(launch.key);
}

function workspaceSortMs(value) {
  const parsed = timestampMs(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

async function attachWorkspaceToCurrent(workspaceId) {
  const agentSession = currentAgentSession();
  if (!agentSession || !workspaceId) return false;
  let workspace = state.sessions.find((item) => item.id === workspaceId);
  if (workspace && workspace.realmManaged && workspace.reopenRequired) {
    workspace = await reopenManagedWorkspace(workspace);
    if (!workspace) return false;
  }
  const attached = state.agentWorkspaceAttachments[agentSession.id] || [];
  if (!attached.includes(workspaceId)) attached.push(workspaceId);
  state.agentWorkspaceAttachments[agentSession.id] = attached;
  setSelectedWorkspace(workspaceId);
  if (workspace && workspace.backendWorkspaceId && !agentSession.id.startsWith("agent-session-")) {
    try {
      const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(agentSession.id)}/attach-workspace`, { workspace_id: workspace.backendWorkspaceId });
      if (payload.session) mergeAgentSessionPayload(payload.session);
    } catch (error) {
      // Keep the optimistic attachment; refresh can reconcile if needed.
    }
  }
  return agentSession;
}

async function reopenManagedWorkspace(workspace) {
  if (!workspace || !workspace.realmManaged) return workspace || null;
  try {
    const payload = await postJson(
      `/api/workspaces/${encodeURIComponent(workspace.backendWorkspaceId || workspace.id)}/reopen`,
      { expected_workspace_revision: workspace.workspaceRevision },
    );
    const reopened = mergeUiWorkspace(payload.workspace);
    if (!reopened) return null;
    state.workspaceNotice = {
      workspaceId: reopened.id,
      title: "Workspace ready",
      body: "OptPilot reopened its editable files.",
      error: false,
    };
    return reopened;
  } catch (error) {
    state.codeWorkspaceStatus = "failed";
    state.codeWorkspaceMessage = `Workspace reopen failed: ${String(error.message || error)}`;
    renderWorkspace();
    return null;
  }
}

function keepWorkspaceSelected(workspaceId) {
  if (!workspaceId) return;
  state.selectedSessionId = workspaceId;
  const agentSession = currentAgentSession();
  if (agentSession) {
    state.selectedWorkspaceByAgentSession[agentSession.id] = workspaceId;
  }
}

function syncSelectedWorkspaceToBackend(workspaceId) {
  const agentSession = currentAgentSession();
  if (!agentSession || agentSession.id.startsWith("agent-session-")) return;
  postJson(`/api/agent-sessions/${encodeURIComponent(agentSession.id)}/select-workspace`, { workspace_id: workspaceId || "" })
    .then((payload) => updateAgentSessionFromPayload(payload.session))
    .catch(() => {});
}

function setSelectedWorkspace(workspaceId, options = {}) {
  if (
    state.workspaceNotice
    && state.workspaceNotice.workspaceId !== (workspaceId || null)
  ) {
    state.workspaceNotice = null;
  }
  state.selectedSessionId = workspaceId || null;
  const attachedToCurrentAssistant = Boolean(
    workspaceId && state.selectedAgentSessionId && attachedWorkspaceIds().includes(workspaceId),
  );
  if (attachedToCurrentAssistant) {
    state.selectedWorkspaceByAgentSession[state.selectedAgentSessionId] = workspaceId || null;
  }
  if (options.sync && attachedToCurrentAssistant) syncSelectedWorkspaceToBackend(workspaceId || "");
  if (state.registrationDraft && state.registrationDraft.workspaceId !== workspaceId) {
    state.registrationDraft = null;
    if (state.assistantMode === "registration") {
      state.assistantMode = "chat";
    }
  }
}

function clearSelectedWorkspaceForPage() {
  if (!state.selectedSessionId) return;
  state.selectedSessionId = null;
  if (state.selectedAgentSessionId) {
    state.selectedWorkspaceByAgentSession[state.selectedAgentSessionId] = null;
  }
  syncSelectedWorkspaceToBackend("");
}

function renderAll() {
  renderNavigation();
  renderPlatformStatus();
  renderActiveInterfaceIndicator();
  renderWorkspace();
  renderCatalog();
  renderExperiments();
  renderRuns();
  renderSelectionContentHost();
  renderAssistant();
  renderSettingsModal();
  renderWorkspaceCleanupModal();
  if (state.selectedRunId && state.view === "runs") {
    loadRunDetail(state.selectedRunId, { keepTab: true });
  }
}

async function openSettings(options = {}) {
  if (options && options.tab) state.settingsTab = options.tab;
  state.settingsOpen = true;
  state.environmentVariableDrafts = [];
  await loadAgentSettings();
  fillSettingsForm();
  renderSettingsModal();
}

function closeSettings() {
  state.settingsOpen = false;
  state.environmentVariableDrafts = [];
  renderSettingsModal();
}

function renderSettingsModal() {
  if (!els.settingsModal) return;
  els.settingsModal.hidden = !state.settingsOpen;
  document.body.classList.toggle("settings-open", state.settingsOpen);
  document.querySelectorAll("[data-settings-tab]").forEach((button) => {
    const active = (button.dataset.settingsTab || "assistant") === state.settingsTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => {
    panel.hidden = (panel.dataset.settingsPanel || "assistant") !== state.settingsTab;
  });
  renderOpenHandsStatus();
}

function fillSettingsForm() {
  const openhands = currentOpenHandsSettings();
  const capabilities = currentAssistantCapabilities();
  const permissions = currentAssistantPermissions();
  if (els.openHandsEnabled) els.openHandsEnabled.checked = Boolean(openhands.enabled);
  if (els.openHandsBaseUrl) els.openHandsBaseUrl.value = openhands.base_url || "";
  if (els.openHandsSessionEndpoint) els.openHandsSessionEndpoint.value = openhands.session_endpoint || "";
  if (els.openHandsModel) els.openHandsModel.value = openhands.model || "";
  if (els.openHandsApiKey) {
    els.openHandsApiKey.value = "";
    els.openHandsApiKey.placeholder = openhands.api_key_configured ? "Configured; leave blank to keep" : "Paste API key";
  }
  if (els.openHandsClearApiKey) els.openHandsClearApiKey.checked = false;
  renderEnvironmentVariablesList();
  if (els.environmentVariableName) els.environmentVariableName.value = "";
  if (els.environmentVariableValue) els.environmentVariableValue.value = "";
  if (els.assistantSkillsInput) els.assistantSkillsInput.value = settingsJson(capabilities.skills || []);
  if (els.assistantMcpServersInput) els.assistantMcpServersInput.value = settingsJson(mcpServersObject(capabilities.mcp_servers || []));
  if (els.assistantMcpFilterRegex) els.assistantMcpFilterRegex.value = capabilities.mcp_filter_regex || "";
  if (els.assistantCustomToolsInput) els.assistantCustomToolsInput.value = settingsJson(capabilities.custom_tools || []);
  setSelectValue(els.assistantPermissionFileWrite, permissions.file_write || "attached_editable");
  setSelectValue(els.assistantPermissionShellRun, permissions.shell_run || "approval_required");
  setSelectValue(els.assistantPermissionCatalogRegistration, permissions.catalog_registration || "approval_required");
  setSelectValue(els.assistantPermissionStudyLaunch, permissions.study_launch || "approval_required");
  setSelectValue(els.assistantPermissionJobStop, permissions.job_stop || "approval_required");
}

function currentOpenHandsSettings() {
  const assistant = state.agentSettings && state.agentSettings.assistant || {};
  return assistant.openhands || {};
}

function currentAssistantCapabilities() {
  const assistant = state.agentSettings && state.agentSettings.assistant || {};
  return assistant.capabilities || { skills: [], mcp_servers: [], custom_tools: [] };
}

function currentAssistantPermissions() {
  const assistant = state.agentSettings && state.agentSettings.assistant || {};
  return assistant.permissions || {};
}

function currentEnvironmentSettings() {
  return state.agentSettings && state.agentSettings.environment || { variables: [] };
}

function environmentVariableRecords() {
  const variables = currentEnvironmentSettings().variables || [];
  const saved = Array.isArray(variables) ? variables : Object.entries(variables).map(([name, value]) => ({ name, configured: Boolean(value) }));
  const records = saved.map((record) => ({ ...record, pending: false }));
  state.environmentVariableDrafts.forEach((draft) => {
    const index = records.findIndex((record) => record.name === draft.name);
    const pendingRecord = { name: draft.name, configured: true, pending: true };
    if (index >= 0) {
      records[index] = { ...records[index], ...pendingRecord };
    } else {
      records.push(pendingRecord);
    }
  });
  return records;
}

function configuredEnvironmentVariableNames() {
  return new Set(environmentVariableRecords().filter((item) => item.configured).map((item) => item.name));
}

function renderEnvironmentVariablesList() {
  if (!els.environmentVariablesList) return;
  const records = environmentVariableRecords();
  if (!records.length) {
    els.environmentVariablesList.innerHTML = `<p class="empty-inline">No Studio environment variables saved yet.</p>`;
    return;
  }
  els.environmentVariablesList.innerHTML = records.map((record) => `
    <label class="env-secret-row">
      <span>
        <strong>${escapeHtml(record.name || "")}</strong>
        <small>${record.pending ? "ready to save" : record.configured ? "stored locally" : "not configured"}</small>
      </span>
      <span class="env-secret-row-actions">
        ${statusPill(record.pending ? "pending" : record.configured ? "configured" : "missing")}
        ${record.pending
          ? `<button class="ghost-button env-draft-remove" data-env-draft-remove="${escapeHtml(record.name || "")}" type="button">Remove</button>`
          : `<span class="checkbox-row">
              <input type="checkbox" data-env-clear="${escapeHtml(record.name || "")}" />
              <span>Remove</span>
            </span>`}
      </span>
    </label>
  `).join("");
}

function addEnvironmentVariableDraft() {
  if (!els.environmentVariableName || !els.environmentVariableValue) return;
  const name = els.environmentVariableName.value.trim();
  const value = els.environmentVariableValue.value;
  els.environmentVariableName.classList.toggle("invalid-input", !name);
  els.environmentVariableValue.classList.toggle("invalid-input", !value);
  if (!name || !value) return;
  const existing = state.environmentVariableDrafts.find((item) => item.name === name);
  if (existing) {
    existing.value = value;
  } else {
    state.environmentVariableDrafts.push({ name, value });
  }
  els.environmentVariableName.value = "";
  els.environmentVariableValue.value = "";
  els.environmentVariableName.classList.remove("invalid-input");
  els.environmentVariableValue.classList.remove("invalid-input");
  renderEnvironmentVariablesList();
}

function environmentSettingsPayload() {
  const set = state.environmentVariableDrafts.map((item) => ({ name: item.name, value: item.value }));
  const name = els.environmentVariableName ? els.environmentVariableName.value.trim() : "";
  const value = els.environmentVariableValue ? els.environmentVariableValue.value : "";
  if (name && value) set.push({ name, value });
  const clear = Array.from(document.querySelectorAll("[data-env-clear]:checked"))
    .map((input) => input.dataset.envClear)
    .filter(Boolean);
  return { set, clear };
}

function settingsJson(value) {
  return JSON.stringify(value || [], null, 2);
}

function setSelectValue(element, value) {
  if (!element) return;
  element.value = value;
  if (element.value !== value && element.options.length) element.selectedIndex = 0;
}

function parseJsonInput(element, fallback, label) {
  if (!element) return fallback;
  element.classList.remove("invalid-input");
  const raw = element.value.trim();
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch (error) {
    element.classList.add("invalid-input");
    throw new Error(`${label} must be valid JSON.`);
  }
}

function mcpServersObject(records) {
  const servers = {};
  (records || []).forEach((record) => {
    const key = record.name || record.id;
    if (!key) return;
    const server = {};
    if (record.url) server.url = record.url;
    if (record.command) server.command = record.command;
    if (record.args && record.args.length) server.args = record.args;
    if (record.auth) server.auth = record.auth;
    if (record.transport) server.transport = record.transport;
    servers[key] = server;
  });
  return servers;
}

function mcpServersFromObject(value) {
  return Object.entries(value || {}).map(([name, config]) => ({
    id: name,
    name,
    ...(config && typeof config === "object" ? config : {}),
    enabled: true,
  }));
}

function renderOpenHandsStatus() {
  const status = state.agentRuntimeStatus || {};
  if (els.assistantLauncherSubtitle) {
    const session = currentAgentSession();
    els.assistantLauncherSubtitle.textContent = session
      ? `${assistantSessionLabel(session)} · ${assistantPublicRuntimeLabel(status)}`
      : assistantPublicRuntimeLabel(status);
  }
  if (!els.openHandsStatus) return;
  const model = status.model || currentOpenHandsSettings().model || "-";
  const server = status.base_url || currentOpenHandsSettings().base_url || "-";
  const keyLabel = status.api_key_configured || currentOpenHandsSettings().api_key_configured ? "API key configured" : "API key missing";
  els.openHandsStatus.innerHTML = `
    <div>
      <strong>${escapeHtml(assistantRuntimeLabel(status))}</strong>
      <span>${escapeHtml(assistantRuntimeDetail(status))}</span>
    </div>
    <div class="settings-status-grid">
      <span>Model</span><strong>${escapeHtml(model)}</strong>
      <span>Server</span><strong>${escapeHtml(server)}</strong>
      <span>Credential</span><strong>${escapeHtml(keyLabel)}</strong>
    </div>
  `;
}

function assistantPublicRuntimeLabel(status) {
  if (!status || status.error) return "Unavailable";
  if (!status.enabled) return "Off";
  if (status.connected || status.reachable) return "Ready";
  if (status.starting) return "Starting";
  return "Unavailable";
}

function assistantRuntimeLabel(status) {
  if (!status || status.error) return "Assistant settings unavailable";
  if (!status.enabled) return "OpenHands disabled";
  if (status.mode === "configured") return status.connected ? "OpenHands ready" : "OpenHands not reachable";
  if (status.mode) return `OpenHands ${status.mode}`;
  return "OpenHands not configured";
}

function assistantRuntimeDetail(status) {
  if (!status || status.error) return "Settings could not be loaded.";
  if (!status.enabled) return "Messages stay local until OpenHands is enabled.";
  if (!status.model) return "Choose a model before sending messages.";
  if (!status.api_key_configured) return "Add an API key before sending messages.";
  if (status.mode === "model chat") return "No agent server URL is configured; messages use the chat fallback.";
  if (status.mode === "configured" && status.connected) return "Runtime dispatch is available.";
  if (status.mode === "configured") return "Agent server is configured but not reachable.";
  if (status.dispatch === "queued") return "Complete assistant settings before sending messages.";
  return "Runtime dispatch is available.";
}

async function saveSettings() {
  let capabilities;
  try {
    const skills = parseJsonInput(els.assistantSkillsInput, [], "AgentSkills");
    const mcpServers = parseJsonInput(els.assistantMcpServersInput, {}, "MCP servers");
    const customTools = parseJsonInput(els.assistantCustomToolsInput, [], "Custom tools");
    capabilities = {
      skills: Array.isArray(skills) ? skills : [],
      mcp_servers: mcpServersFromObject(mcpServers && typeof mcpServers === "object" && !Array.isArray(mcpServers) ? mcpServers : {}),
      mcp_filter_regex: els.assistantMcpFilterRegex ? els.assistantMcpFilterRegex.value.trim() : "",
      custom_tools: Array.isArray(customTools) ? customTools : [],
    };
  } catch (error) {
    state.agentRuntimeStatus = { runtime: "openhands", enabled: false, mode: "settings error", error: String(error.message || error) };
    renderOpenHandsStatus();
    return;
  }
  const payload = {
    openhands: {
      enabled: Boolean(els.openHandsEnabled && els.openHandsEnabled.checked),
      base_url: els.openHandsBaseUrl ? els.openHandsBaseUrl.value.trim() : "",
      session_endpoint: els.openHandsSessionEndpoint ? els.openHandsSessionEndpoint.value.trim() : "",
      model: els.openHandsModel ? els.openHandsModel.value.trim() : "",
      api_key: els.openHandsApiKey ? els.openHandsApiKey.value.trim() : "",
      clear_api_key: Boolean(els.openHandsClearApiKey && els.openHandsClearApiKey.checked),
    },
    capabilities,
    environment: environmentSettingsPayload(),
    permissions: {
      file_write: els.assistantPermissionFileWrite ? els.assistantPermissionFileWrite.value : "attached_editable",
      shell_run: els.assistantPermissionShellRun ? els.assistantPermissionShellRun.value : "approval_required",
      catalog_registration: els.assistantPermissionCatalogRegistration ? els.assistantPermissionCatalogRegistration.value : "approval_required",
      study_launch: els.assistantPermissionStudyLaunch ? els.assistantPermissionStudyLaunch.value : "approval_required",
      job_stop: els.assistantPermissionJobStop ? els.assistantPermissionJobStop.value : "approval_required",
    },
  };
  const result = await postJson("/api/agent/settings", payload, { tolerateError: true });
  if (result.error) {
    state.agentRuntimeStatus = { runtime: "openhands", enabled: false, mode: "unavailable", error: result.error };
  } else {
    state.agentSettings = result.settings || state.agentSettings;
    state.agentRuntimeStatus = result.status || state.agentRuntimeStatus;
    state.environmentVariableDrafts = [];
  }
  fillSettingsForm();
  renderSettingsModal();
  renderPlatformStatus();
  renderCatalog();
  renderExperiments();
  renderAssistant();
}

function parseStudioRoute(hash = window.location.hash) {
  const value = String(hash || "").replace(/^#\/?/, "");
  if (!value) return null;
  const parts = value.split("/").filter(Boolean).map((part) => {
    try {
      return decodeURIComponent(part);
    } catch (error) {
      return "";
    }
  });
  const page = parts[0];
  if (page === "workspaces") return { view: "workspace", workspaceId: parts[1] || "" };
  if (page === "catalog") return { view: "catalog", componentKey: parts[1] || "" };
  if (page === "studies") return { view: "experiments", planId: parts[1] || "" };
  if (page === "runs") {
    return {
      view: "runs",
      runId: parts[1] || "",
      candidateId: parts[2] === "candidates" ? parts[3] || "" : "",
    };
  }
  return null;
}

function studioRouteHash() {
  const segment = (value) => encodeURIComponent(String(value || ""));
  if (state.view === "workspace") {
    if (isCatalogSourceView() && state.selectedComponentKey) {
      return `#/catalog/${segment(state.selectedComponentKey)}`;
    }
    return state.selectedSessionId ? `#/workspaces/${segment(state.selectedSessionId)}` : "#/workspaces";
  }
  if (state.view === "catalog") {
    return state.selectedComponentKey ? `#/catalog/${segment(state.selectedComponentKey)}` : "#/catalog";
  }
  if (state.view === "experiments") {
    return state.selectedPlanId ? `#/studies/${segment(state.selectedPlanId)}` : "#/studies";
  }
  if (state.view === "runs") {
    if (!state.selectedRunId) return "#/runs";
    const run = `#/runs/${segment(state.selectedRunId)}`;
    return state.routedCandidateId ? `${run}/candidates/${segment(state.routedCandidateId)}` : run;
  }
  return "#/workspaces";
}

function syncStudioRoute() {
  const hash = studioRouteHash();
  if (window.location.hash === hash) return;
  window.history.replaceState(null, "", hash);
}

function applyStudioRoute(options = {}) {
  const route = parseStudioRoute();
  if (!route) {
    if (options.render !== false) renderAll();
    return "";
  }
  state.view = route.view;
  if (route.view === "workspace" && state.sessions.some((item) => item.id === route.workspaceId)) {
    state.selectedSessionId = route.workspaceId;
  } else if (route.view === "catalog" && allComponents().some((item) => item.key === route.componentKey)) {
    state.selectedComponentKey = route.componentKey;
  } else if (route.view === "experiments" && state.plans.some((item) => item.id === route.planId)) {
    state.selectedPlanId = route.planId;
  } else if (route.view === "runs") {
    state.selectedRunId = route.runId || state.selectedRunId;
    const candidateId = route.candidateId || null;
    if (candidateId !== state.routedCandidateId) {
      state.routedCandidateResolution = null;
      state.routedCandidateFocusApplied = "";
    }
    state.routedCandidateId = candidateId;
    if (candidateId) state.activeRunTab = "candidate";
  }
  if (options.render !== false) renderAll();
  if (options.loadRun && route.view === "runs" && route.runId) {
    loadRunDetail(route.runId, { keepTab: true, skipListRender: true, fromRoute: true }).catch(() => {});
  }
  return route.view === "runs" ? route.runId || "" : "";
}

function setView(view, options = {}) {
  if (view !== "runs" && state.pendingCandidateTry) {
    closeCandidateTrySheet({ restoreFocus: false });
  }
  if (view === "workspace") {
    const selected = currentSession();
    if (!selected || (selected.visibleInWorkspaces === false && options.allowSupportView !== true)) {
      const firstWorkspace = orderedWorkspaceSessions()[0] || null;
      setSelectedWorkspace(firstWorkspace && firstWorkspace.id || null);
    }
  }
  if (view !== "runs") {
    state.routedCandidateId = null;
    state.routedCandidateResolution = null;
    state.routedCandidateFocusApplied = "";
  }
  state.view = view;
  renderNavigation();
  if (view === "workspace") renderWorkspace();
  if (view !== "workspace") renderWorkspace();
  if (view === "catalog") renderCatalog();
  if (view === "experiments") renderExperiments();
  if (view === "runs") {
    renderRuns();
    if (shouldRefreshSelectedRunDetail()) {
      loadRunDetail(state.selectedRunId, { keepTab: true, skipListRender: true });
    }
  }
  renderAssistant();
  if (!options.fromRoute) syncStudioRoute();
}

function setWorkbenchMode(mode) {
  state.workbenchMode = ["code", "preview", "setup"].includes(mode) ? mode : "code";
  renderWorkbenchMode();
  if (state.workbenchMode === "preview") renderPreviewWorkbench();
  if (state.workbenchMode === "setup") renderWorkspaceSetup();
}

function toggleAssistant() {
  setAssistantOpen(!state.assistantOpen);
}

function setAssistantOpen(open) {
  state.assistantOpen = Boolean(open);
  if (state.assistantOpen && !state.assistantMode) state.assistantMode = "chat";
  renderAssistant();
}

function renderAssistant() {
  document.body.classList.toggle("assistant-open", state.assistantOpen);
  document.body.classList.toggle("assistant-session-list-open", state.assistantOpen && state.assistantMode === "sessions");
  document.documentElement.style.setProperty("--assistant-panel-width", `${state.assistantPanelWidth}px`);
  if (els.assistantToggleButton) {
    els.assistantToggleButton.classList.toggle("active", state.assistantOpen);
    els.assistantToggleButton.setAttribute("aria-expanded", String(state.assistantOpen));
  }
  const session = currentAgentSession();
  const isSessionList = state.assistantMode === "sessions";
  const attachedCount = session ? attachedWorkspaceIds(session.id).length : 0;
  const pageLabel = currentViewLabel();
  const isRegistration = state.assistantMode === "registration";
  const nextApprovalKey = session && !isSessionList && !isRegistration ? pendingApprovalKeyForSession(session.id) : "";
  const previousApprovalKey = session ? state.assistantApprovalKeysBySession[session.id] || "" : "";
  const shouldScrollToApprovals = state.assistantOpen && Boolean(nextApprovalKey) && nextApprovalKey !== previousApprovalKey;
  if (els.assistantBackButton) els.assistantBackButton.hidden = isSessionList;
  if (els.assistantTitle) {
    els.assistantTitle.textContent = isSessionList ? "Assistant conversations" : isRegistration ? "Publish to Catalog" : session ? assistantSessionLabel(session) : "OptPilot Assistant";
  }
  if (els.assistantSubtitle) {
    els.assistantSubtitle.textContent = isSessionList
      ? "Resume a conversation or start a new one"
      : isRegistration
        ? "Find configurations, check files, and publish Catalog items"
        : session
          ? `Conversation · ${attachedCount} Workspace${attachedCount === 1 ? "" : "s"} available`
          : "Start or select an Assistant conversation";
    els.assistantSubtitle.hidden = !els.assistantSubtitle.textContent;
  }
  if (els.assistantContextHint) {
    els.assistantContextHint.textContent = assistantContextSummary();
  }
  if (els.assistantSessionList) els.assistantSessionList.hidden = !isSessionList;
  if (els.agentTimeline) {
    els.agentTimeline.hidden = isSessionList;
    els.agentTimeline.innerHTML = isRegistration ? registrationMenuHtml() : assistantTimelineHtml(session);
  }
  const composer = document.querySelector(".agent-panel .composer");
  if (composer) composer.hidden = isSessionList;
  updateAssistantInputPlaceholder();
  updateAssistantComposerState();
  renderOpenHandsStatus();
  renderAssistantSessionList();
  bindAssistantApprovals();
  bindRegistrationMenu();
  if (session && !isSessionList && !isRegistration) {
    state.assistantApprovalKeysBySession[session.id] = nextApprovalKey;
  }
  queueAssistantStepAutoScroll();
  if (shouldScrollToApprovals) queueAssistantApprovalAutoScroll();
}

function queueAssistantStepAutoScroll() {
  if (!els.agentTimeline) return;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(scrollWorkingAssistantStepsToBottom);
  });
}

function scrollWorkingAssistantStepsToBottom() {
  if (!els.agentTimeline) return;
  els.agentTimeline.querySelectorAll(".assistant-step-group.working .assistant-step-scroll").forEach((scroller) => {
    scroller.scrollTop = scroller.scrollHeight;
  });
}

function queueAssistantApprovalAutoScroll() {
  if (!els.agentTimeline) return;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(scrollAssistantApprovalIntoView);
  });
}

function scrollAssistantApprovalIntoView() {
  if (!els.agentTimeline) return;
  const cards = els.agentTimeline.querySelectorAll(".approval-card");
  const lastCard = cards[cards.length - 1];
  if (!lastCard) return;
  lastCard.scrollIntoView({ block: "end", inline: "nearest" });
  els.agentTimeline.scrollTop = els.agentTimeline.scrollHeight;
}

function assistantTimelineHtml(session) {
  return `${assistantInterleavedTimelineHtml(session)}${assistantApprovalsHtml()}`;
}

function assistantApprovalsHtml() {
  const approvals = currentAssistantApprovals();
  if (!approvals.length) return "";
  const session = currentAgentSession();
  const queuedCount = Number(session && session.queued_approval_count || 0);
  return `
    <div class="approval-stack">
      ${approvals.map((approval) => `
        <div class="approval-card">
          <div>
            <span>${escapeHtml(approval.kind || "approval")}</span>
            <strong>${escapeHtml(approval.title || "Assistant paused for approval")}</strong>
            <p>${escapeHtml((approval.display_payload && approval.display_payload.summary) || approval.summary || "")}</p>
            ${((approval.display_payload && approval.display_payload.targets) || approval.targets || []).length ? `<small>${escapeHtml(((approval.display_payload && approval.display_payload.targets) || approval.targets || []).join(" - "))}</small>` : ""}
            ${queuedCount ? `<small>${escapeHtml(`${queuedCount} more approval request${queuedCount === 1 ? "" : "s"} queued after this one.`)}</small>` : ""}
          </div>
          <div class="approval-actions">
            <button class="ghost-button" data-reject-approval="${escapeHtml(approval.id)}" type="button">Reject</button>
            <button class="primary-button" data-approve-approval="${escapeHtml(approval.id)}" type="button">Approve</button>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

function assistantInterleavedTimelineHtml(session) {
  const messages = currentAssistantMessages();
  const events = currentAssistantEvents()
    .map((event, index) => ({ ...event, __index: index }))
    .sort((left, right) => {
      const byTime = eventTimestampMs(left) - eventTimestampMs(right);
      return Number.isFinite(byTime) && byTime !== 0 ? byTime : left.__index - right.__index;
    });
  if (!messages.length) return "";
  const html = [];
  const messageTimes = messages.map(messageTimestampMs);
  const renderedEventIndexes = new Set();
  messages.forEach((message, index) => {
    html.push(timelineItem(message));
    if (message[0] !== "user") return;
    const messageTime = messageTimes[index];
    if (!Number.isFinite(messageTime)) return;
    const nextUserIndex = messages
      .slice(index + 1)
      .findIndex((candidate) => candidate[0] === "user");
    const turnEndIndex = nextUserIndex === -1 ? messages.length : index + 1 + nextUserIndex;
    const turnMessages = messages.slice(index + 1, turnEndIndex);
    const hasAssistantReply = turnMessages.some((candidate) => candidate[0] === "assistant" || candidate[0] === "agent");
    const isLatestUserTurn = turnEndIndex === messages.length;
    const isWorking = isLatestUserTurn && !hasAssistantReply && Boolean(session && ["waiting_for_agent", "running", "resuming_after_approval"].includes(assistantSessionStatus(session)));
    const nextUserTime = messages
      .slice(index + 1)
      .filter((candidate) => candidate[0] === "user")
      .map(messageTimestampMs)
      .find(Number.isFinite) ?? Number.POSITIVE_INFINITY;
    const turnEvents = events.filter((event) => {
      if (renderedEventIndexes.has(event.__index)) return false;
      const eventTime = eventTimestampMs(event);
      return Number.isFinite(eventTime) && eventTime >= messageTime && eventTime < nextUserTime;
    });
    turnEvents.forEach((event) => renderedEventIndexes.add(event.__index));
    if (turnEvents.length || isWorking) {
      html.push(assistantStepGroupHtml(turnEvents, { isWorking, open: isWorking }));
    }
  });
  return html.join("");
}

function assistantStepGroupHtml(events, options = {}) {
  const visibleEvents = events.filter(assistantEventIsInformative);
  if (!visibleEvents.length && !options.isWorking) return "";
  const start = firstFinite(visibleEvents.map(eventTimestampMs));
  const end = lastFinite(visibleEvents.map(eventTimestampMs));
  const label = options.isWorking
    ? "Working"
    : Number.isFinite(start) && Number.isFinite(end)
    ? `Worked for ${formatDuration(Math.max(0, end - start))}`
    : `${visibleEvents.length} assistant step${visibleEvents.length === 1 ? "" : "s"}`;
  return `
    <details class="assistant-step-group ${options.isWorking ? "working" : ""}" ${options.open ? "open" : ""}>
      <summary>
        ${options.isWorking ? assistantTypingDotsHtml() : ""}
        <span>${escapeHtml(label)}</span>
        <strong>${visibleEvents.length}</strong>
      </summary>
      <div class="assistant-step-scroll">
        ${visibleEvents.length ? `
          <ol>
            ${visibleEvents.map((event) => {
              const step = assistantStepSummary(event);
              return `
                <li class="${escapeHtml(step.status)}">
                  <span>${escapeHtml(step.time)}</span>
                  <div>
                    <strong>${escapeHtml(step.title)}</strong>
                    ${step.detail ? `<p>${escapeHtml(step.detail)}</p>` : ""}
                    ${step.codeBlock ? `<pre class="assistant-step-pre">${escapeHtml(step.codeBlock)}</pre>` : ""}
                    <code>${escapeHtml(step.type)}</code>
                  </div>
                </li>
              `;
            }).join("")}
          </ol>
        ` : `<p class="assistant-step-empty">Waiting for intermediate steps...</p>`}
      </div>
    </details>
  `;
}

function assistantTypingDotsHtml() {
  return `
    <span class="typing-dots" aria-hidden="true">
      <i></i>
      <i></i>
      <i></i>
    </span>
  `;
}

function assistantEventIsInformative(event) {
  if (!event || typeof event !== "object") return false;
  const type = event.type || "";
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  if (type === "optpilot_tool_result") return true;
  if (type === "openhands_event") {
    const category = payload.category || "";
    if (String(payload.summary || "").startsWith("OptPilot tool result for ")) return false;
    return ["reasoning", "tool_call", "user_message", "error"].includes(category) || Boolean(payload.tool || payload.reasoning);
  }
  if (type === "approval_requested" || type === "approval_approved" || type === "approval_rejected") return true;
  if (type === "workspace_attached" || type === "workspace_detached") return true;
  if (type === "openhands_dispatch_cancelled") return true;
  if (type === "openhands_cancel_acknowledged" || type === "openhands_cancel_failed") return true;
  if (type === "openhands_tool_result_forwarded" || type === "openhands_tool_result_forward_skipped") return true;
  return type.includes("failed") || type.includes("error");
}

function assistantStepSummary(event) {
  const payload = event && typeof event.payload === "object" && event.payload ? event.payload : {};
  const type = event && event.type || "backend_event";
  const base = {
    status: eventStatus(event),
    time: formatEventTime(event && event.created_at),
    type,
    title: humanizeEventType(type),
    detail: payloadPreview(payload),
  };
  if (event.type === "optpilot_tool_result") {
    return {
      ...base,
      title: payload.tool ? `Tool result: ${payload.tool}` : "Tool result",
      detail: payload.summary || (payload.ok === false ? "Tool failed." : "Tool completed."),
      codeBlock: payload.result_preview || "",
    };
  }
  if (event.type === "openhands_event") {
    const category = payload.category || "";
    if (category === "reasoning") {
      return {
        ...base,
        title: "Reasoning",
        detail: payload.reasoning || payload.summary || "",
        codeBlock: "",
      };
    }
    if (category === "tool_call" || payload.tool) {
      return {
        ...base,
        title: payload.tool ? `Tool call: ${payload.tool}` : "Tool call",
        detail: payload.reasoning || (payload.tool_call_id ? `Call ${payload.tool_call_id}` : ""),
        codeBlock: payload.arguments_preview || "",
      };
    }
    if (category === "user_message") {
      return {
        ...base,
        title: "User request sent",
        detail: payload.summary || "",
        codeBlock: "",
      };
    }
    if (category === "error") {
      return {
        ...base,
        title: "OpenHands error",
        detail: payload.summary || payload.raw_preview || "",
        codeBlock: "",
      };
    }
    return {
      ...base,
      title: payload.event_type ? `OpenHands ${payload.event_type}` : "OpenHands event",
      detail: payload.summary || payload.raw_preview || "",
    };
  }
  if (event.type === "workspace_attached") {
    return { ...base, title: "Workspace added to this conversation", detail: payload.workspace_id || "" };
  }
  if (event.type === "workspace_detached") {
    return { ...base, title: "Workspace removed from this conversation", detail: payload.workspace_id || "" };
  }
  if (event.type === "approval_requested") {
    return { ...base, title: payload.title || "Approval requested", detail: payload.summary || payload.tool || "" };
  }
  if (event.type === "approval_approved") {
    return { ...base, title: "Approval approved", detail: payload.tool || "" };
  }
  if (event.type === "approval_rejected") {
    return { ...base, title: "Approval rejected", detail: payload.reason || payload.tool || "" };
  }
  if (event.type === "openhands_tool_result_forwarded") {
    return { ...base, title: "Tool result sent to OpenHands", detail: payload.tool || payload.tool_call_id || "" };
  }
  if (event.type === "openhands_tool_result_forward_skipped") {
    return { ...base, title: "Tool result kept in Studio", detail: payload.reason || payload.tool || "" };
  }
  if (event.type === "openhands_dispatch_failed") {
    return { ...base, title: "OpenHands dispatch failed", detail: payload.error || "" };
  }
  if (event.type === "openhands_dispatch_queued") {
    return { ...base, title: "OpenHands dispatch queued", detail: payload.mode || "" };
  }
  if (event.type === "openhands_dispatch_started") {
    return { ...base, title: "OpenHands dispatch started", detail: payload.dispatch || payload.mode || "" };
  }
  if (event.type === "openhands_dispatch_completed") {
    return { ...base, title: "OpenHands dispatch completed", detail: payload.dispatch || payload.status || "" };
  }
  if (event.type === "openhands_dispatch_cancelled") {
    const detail = payload.remote_cancelled
      ? `Interrupted OpenHands${payload.remote_action ? ` via ${payload.remote_action}` : ""}.`
      : (payload.remote_cancel_scheduled ? "Stopped locally. Interrupting OpenHands in the background." : (payload.remote_error || "Stopped locally."));
    return { ...base, title: "Assistant stopped", detail };
  }
  if (event.type === "openhands_cancel_acknowledged") {
    const detail = payload.remote_action ? `OpenHands accepted ${payload.remote_action}.` : "OpenHands accepted the interrupt.";
    return { ...base, title: "OpenHands interrupt acknowledged", detail };
  }
  if (event.type === "openhands_cancel_failed") {
    return { ...base, title: "OpenHands interrupt failed", detail: payload.remote_error || "Studio stopped locally, but OpenHands did not acknowledge the interrupt." };
  }
  if (event.type === "openhands_chat_completion_completed") {
    return { ...base, title: "OpenHands chat completed", detail: payload.conversation_id || "" };
  }
  if (event.type === "openhands_model_chat_completed") {
    return { ...base, title: "Model chat completed", detail: payload.model || "" };
  }
  if (event.type === "message") {
    return { ...base, title: `${capitalize(payload.role || "assistant")} message stored`, detail: payload.message_id || "" };
  }
  if (event.type === "session_created") {
    return { ...base, title: "Conversation created", detail: payload.title || "" };
  }
  return base;
}

function eventStatus(event) {
  const type = String(event && event.type || "");
  const payload = event && typeof event.payload === "object" && event.payload ? event.payload : {};
  if (payload.ok === false || type.includes("failed") || type.includes("rejected") || type.includes("error")) return "failed";
  if (type.includes("requested") || type.includes("queued")) return "waiting";
  if (type.includes("started") || type.includes("running")) return "running";
  return "done";
}

function eventTimestampMs(event) {
  return timestampMs(event && (event.created_at || event.createdAt));
}

function messageTimestampMs(message) {
  const meta = message && message[3] && typeof message[3] === "object" ? message[3] : {};
  return timestampMs(meta.createdAt || meta.created_at);
}

function timestampMs(value) {
  if (!value) return Number.POSITIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
}

function firstFinite(values) {
  return values.find(Number.isFinite) ?? Number.POSITIVE_INFINITY;
}

function lastFinite(values) {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (Number.isFinite(values[index])) return values[index];
  }
  return Number.POSITIVE_INFINITY;
}

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "";
  if (ms < 1000) return "<1s";
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function formatEventTime(value) {
  const ms = timestampMs(value);
  if (!Number.isFinite(ms)) return "";
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function humanizeEventType(type) {
  return String(type || "backend_event")
    .replace(/^openhands_/, "OpenHands ")
    .replace(/^optpilot_/, "OptPilot ")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function payloadPreview(payload) {
  if (!payload || typeof payload !== "object" || !Object.keys(payload).length) return "";
  const text = JSON.stringify(payload);
  return text.length > 220 ? `${text.slice(0, 220)}...` : text;
}

function capitalize(value) {
  const text = String(value || "");
  return text ? `${text[0].toUpperCase()}${text.slice(1)}` : "";
}

function bindAssistantApprovals() {
  const session = currentAgentSession();
  if (!session || !session.id || session.id.startsWith("agent-session-")) return;
  document.querySelectorAll("[data-approve-approval]").forEach((button) => {
    button.addEventListener("click", async () => {
      await resolveAssistantApproval(session.id, button.dataset.approveApproval, "approve");
    });
  });
  document.querySelectorAll("[data-reject-approval]").forEach((button) => {
    button.addEventListener("click", async () => {
      await resolveAssistantApproval(session.id, button.dataset.rejectApproval, "reject");
    });
  });
}

async function resolveAssistantApproval(sessionId, approvalId, action) {
  if (!approvalId) return;
  const selector = action === "approve"
    ? `[data-approve-approval="${cssEscape(approvalId)}"]`
    : `[data-reject-approval="${cssEscape(approvalId)}"]`;
  const card = document.querySelector(selector) && document.querySelector(selector).closest(".approval-card");
  if (card) card.classList.add("is-resolving");
  try {
    const payload = await postJson(
      `/api/agent-sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}/${action}`,
      action === "reject" ? { reason: "Rejected in the assistant panel." } : {},
    );
    if (payload.approval) {
      const approvals = state.agentApprovalsBySession[sessionId] || [];
      state.agentApprovalsBySession[sessionId] = approvals.map((item) => item.id === approvalId ? payload.approval : item);
    }
    if (payload.session) {
      await updateAgentSessionFromPayload(payload.session);
    } else {
      await loadAgentSessions();
      renderAssistant();
    }
    await refreshAgentWorkspaceState();
  } catch (error) {
    pushAssistantMessage(["tool", "Approval failed", String(error.message || error)]);
    renderAssistant();
  }
}

function updateAssistantInputPlaceholder() {
  if (!els.agentInput) return;
  els.agentInput.placeholder = assistantPromptForContext();
}

function handleAgentInputKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (assistantIsBusy() || assistantIsAwaitingApproval()) return;
    sendAgentMessage();
  }
}

function updateAssistantComposerState() {
  if (!els.sendAgentButton) return;
  const busy = assistantIsBusy();
  const awaitingApproval = assistantIsAwaitingApproval();
  const session = currentAgentSession();
  const cancelling = Boolean(session && state.cancellingAgentSessionIds.has(session.id));
  if (els.agentInput) {
    els.agentInput.disabled = awaitingApproval;
    if (awaitingApproval) {
      els.agentInput.placeholder = "Resolve the pending approval before sending another message.";
    } else {
      updateAssistantInputPlaceholder();
    }
  }
  els.sendAgentButton.disabled = cancelling || awaitingApproval;
  els.sendAgentButton.classList.toggle("stopping", busy);
  els.sendAgentButton.setAttribute("aria-label", awaitingApproval ? "Approval required" : busy ? "Stop assistant" : "Send message");
  els.sendAgentButton.setAttribute("title", awaitingApproval ? "Resolve the pending approval first" : busy ? "Stop assistant" : "Send message");
  els.sendAgentButton.innerHTML = busy
    ? `<span aria-hidden="true" class="stop-icon"></span>`
    : `<span aria-hidden="true">&uarr;</span>`;
}

function assistantIsBusy() {
  const session = currentAgentSession();
  return Boolean(session && ["waiting_for_agent", "running", "resuming_after_approval"].includes(assistantSessionStatus(session)));
}

function assistantIsAwaitingApproval() {
  const session = currentAgentSession();
  return Boolean(session && assistantSessionStatus(session) === "awaiting_user_approval");
}

function assistantPromptForContext() {
  if (state.assistantOpen && state.assistantMode === "registration") {
    return "Help me choose, check, and publish the right Catalog items from this Workspace.";
  }
  if (state.view === "runs") {
    const runName = state.selectedRun && state.selectedRun.run && state.selectedRun.run.name;
    return runName
      ? `Summarize evidence for ${runName}, compare candidates, and explain failures or metrics.`
      : "Summarize the selected run, compare candidates, and inspect failures or artifacts.";
  }
  if (state.view === "catalog") return "Help me inspect this Catalog item or open it as an editable Workspace.";
  if (state.view === "experiments") return "Help me configure this Study, check it, and prepare it to launch a Run.";
  const workspace = currentSession();
  const agentSession = currentAgentSession();
  if (
    workspace
    && agentSession
    && !attachedWorkspaceIds(agentSession.id).includes(workspace.id)
  ) {
    return `Ask a general question. To work with these files, choose Ask in ${assistantSessionLabel(agentSession)} from Workspace actions.`;
  }
  return "Help me inspect this Workspace, edit code, check configurations, or publish Catalog items.";
}

function assistantContextSummary() {
  const parts = [`Viewing ${currentViewLabel()}`];
  if (state.view === "catalog") {
    const component = componentByKey(state.selectedComponentKey);
    parts.push(component ? `Catalog item: ${component.entry.label} (${component.kind})` : "No Catalog item selected");
  } else if (state.view === "experiments") {
    const plan = currentPlan();
    parts.push(plan ? `Study: ${plan.title}` : "No Study selected");
  } else if (state.view === "runs") {
    const run = selectedRunSummary();
    parts.push(run ? `Run: ${run.name || run.id}${run.status ? ` (${run.status})` : ""}` : "No run selected");
    const selected = state.assistantRunSelection;
    if (run && selected && selected.run_id === canonicalRunId(run)) {
      const session = currentAgentSession();
      if (session && selected.session_id === session.id) {
        parts.push(`Selection: ${selected.kind} ${selected.id}`);
      }
    }
  } else {
    const workspace = currentSession();
    const agentSession = currentAgentSession();
    parts.push(workspace ? `Current page: ${workspace.title}` : "No Workspace open");
    if (workspace && agentSession) {
      parts.push(
        attachedWorkspaceIds(agentSession.id).includes(workspace.id)
          ? `Files available to ${assistantSessionLabel(agentSession)}`
          : `Files not available to ${assistantSessionLabel(agentSession)}`,
      );
    }
  }
  return parts.join(" · ");
}

function selectedRunSummary() {
  if (state.selectedRun && state.selectedRun.run) return state.selectedRun.run;
  return state.runs.find((run) => canonicalRunId(run) === state.selectedRunId) || null;
}

function renderNavigation() {
  const catalogSourceView = state.view === "workspace" && isCatalogSourceView();
  ["workspace", "catalog", "experiments", "runs"].forEach((view) => {
    document.body.classList.toggle(`view-${view}`, state.view === view);
  });
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === (catalogSourceView ? "catalog" : state.view));
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active-view", section.id === `${state.view}View`);
  });
  const titles = {
    workspace: ["Workspaces", "Edit code, run its interface, and publish reusable versions."],
    catalog: ["Catalog", "Reusable environments, methods, and resources."],
    experiments: ["Studies", "Configure and launch optimization Runs."],
    runs: ["Runs", "Progress, metrics, Candidates, trials, and saved results."],
  };
  els.pageTitle.textContent = catalogSourceView ? "Catalog item" : titles[state.view][0];
  els.pageSubtitle.textContent = catalogSourceView
    ? "Read-only files from the exact published Catalog version."
    : titles[state.view][1];
}

function currentViewLabel() {
  return {
    workspace: "Workspaces",
    catalog: "Catalog",
    experiments: "Studies",
    runs: "Runs",
  }[state.view] || "Editor";
}

function buildSessions() {
  return (state.uiWorkspaces || []).map(uiWorkspaceSession);
}

function uiWorkspaceSession(workspace) {
  const entries = workspace.registered_entries || [];
  const primary = entries[0] || null;
  const focusFiles = workspace.focus_paths && workspace.focus_paths.length ? workspace.focus_paths : ["README.md"];
  const files = {};
  focusFiles.slice(0, 6).forEach((path, index) => {
    files[`file${index}`] = {
      label: path,
      state: workspace.mode === "read-only" || workspace.mode === "analysis" ? "read-only" : "editable",
      content: `# ${path}\n\nOpen this workspace in embedded or separate Code Server to inspect the live file contents.\n`,
    };
  });
  if (!Object.keys(files).length) {
    files.notes = {
      label: "workspace_notes.md",
      state: "draft",
      content: "# Workspace Notes\n\nNo focus files have been recorded for this workspace yet.\n",
    };
  }
  const sourceType = workspace.source_type || "workspace";
  const hiddenProviderPath = workspace.provider_path_hidden === true;
  const workspacePathLabel = hiddenProviderPath
    ? "Connected local folder"
    : shortPath(workspace.root || workspace.source_path || "");
  const contextKind = workspace.mode === "read-only"
    ? "Read-only Catalog item"
    : sourceType === "configured-catalog-source"
    ? "Linked local folder"
    : primary
    ? `${fieldLabel(primary.kind)} Workspace`
    : "Workspace files";
  return {
    id: workspace.id,
    backendWorkspaceId: workspace.id,
    kind: primary ? primary.kind : sourceType,
    mode: workspace.mode || "editable",
    visibleInWorkspaces: workspace.visible_in_workspaces !== false,
    sourceType,
    title: workspace.title || "Workspace",
    status: workspace.status || (entries.length ? "registered" : "ready"),
    target: hiddenProviderPath ? workspacePathLabel : workspace.source_path || workspace.root,
    path: workspacePathLabel,
    ideFolder: hiddenProviderPath ? workspacePathLabel : shortPath(workspace.root || ""),
    codeFolder: workspace.root || "",
    context: [contextKind, workspaceCatalogStatusFromRecord(workspace)],
    tools: workspaceCapabilities(workspace),
    registrationEnabled: workspace.registration_enabled !== false,
    registeredEntries: entries,
    catalogPublications: workspace.catalog_publications || [],
    catalogOrigin: workspace.catalog_origin || null,
    interface: workspace.interface || null,
    attachedSessions: workspace.attached_sessions || [],
    ownership: workspace.ownership || (workspace.managed_by_studio ? "studio-owned" : "external-reference"),
    realmManaged: workspace.ownership === "realm-managed",
    workspaceRevision: Number(workspace.realm_workspace_revision || 0) || null,
    workspaceMetadataRevision: Number(workspace.realm_workspace_metadata_revision || 0) || null,
    reopenRequired: Boolean(workspace.reopen_required),
    realizationState: workspace.realization_state || (workspace.reopen_required ? "closed" : "open"),
    lastCommitStatus: workspace.last_commit_status || "",
    managedByStudio: Boolean(workspace.managed_by_studio),
    deleteAction: workspace.delete_action || (workspace.managed_by_studio ? "delete_draft" : "remove_reference"),
    deleteLabel: workspace.delete_label || (
      workspace.ownership === "external-reference"
        ? "Remove from Workspaces"
        : "Delete Workspace"
    ),
    runtime: workspace.runtime || null,
    updatedAt: workspace.updated_at || workspace.created_at || "",
    createdAt: workspace.created_at || "",
    files,
    lenses: [["Source", sourceType], ["Mode", workspace.mode || "editable"], ["Catalog", workspaceCatalogStatusFromRecord(workspace)]],
    timeline: [["workspace", "Workspace ready", workspace.description || "Editable Workspace files are available in Studio."]],
    terminal: workspace.registration_enabled === false
      ? ["$ optpilot inspect-run", `root: ${workspacePathLabel}`]
      : ["$ optpilot discover-configs", `root: ${workspacePathLabel}`],
    checks: [
      ["Workspace folder", workspacePathLabel, "ready"],
      ["Storage", workspaceStorageLabelFromRecord(workspace), "ready"],
      ["Runtime", workspace.runtime && workspace.runtime.status || "unavailable", workspace.runtime && workspace.runtime.containerized ? "ready" : "review"],
      ["Catalog", workspaceCatalogStatusFromRecord(workspace), (workspace.catalog_publications || []).length ? "ready" : "review"],
    ],
  };
}

function mergeUiWorkspace(workspace) {
  if (!workspace || !workspace.id) return null;
  if (workspace.deleted) {
    state.uiWorkspaces = state.uiWorkspaces.filter((item) => item.id !== workspace.id);
    state.sessions = state.sessions.filter((item) => item.id !== workspace.id);
    Object.keys(state.agentWorkspaceAttachments).forEach((sessionId) => {
      state.agentWorkspaceAttachments[sessionId] = (state.agentWorkspaceAttachments[sessionId] || []).filter((id) => id !== workspace.id);
    });
    if (state.selectedSessionId === workspace.id) state.selectedSessionId = null;
    return null;
  }
  state.uiWorkspaces = [workspace, ...state.uiWorkspaces.filter((item) => item.id !== workspace.id)];
  const session = uiWorkspaceSession(workspace);
  upsertSession(session);
  return session;
}

function workspaceCapabilities(workspace) {
  if (Array.isArray(workspace.tools) && workspace.tools.length) {
    return workspace.tools.map((tool) => typeof tool === "string" ? { label: tool, status: "available" } : tool);
  }
  if (workspace.registration_enabled === false || workspace.source_type === "run") {
    return [
      { label: "Browse artifacts", status: "available" },
      { label: "Analyze results", status: "available" },
      { label: "Open Code Server", status: "available" },
    ];
  }
  if (workspace.mode === "read-only") {
    return [
      { label: "Inspect source", status: "available" },
      { label: "Open Code Server", status: "available" },
    ];
  }
  return [
    { label: "Discover configs", status: "available" },
    { label: "Prepare publishing", status: "available" },
    { label: "Open preview", status: "optional" },
  ];
}

function savedStudyDraftPlan(saved) {
  const config = saved && saved.config && typeof saved.config === "object" ? saved.config : {};
  const availability = saved && saved.availability && typeof saved.availability === "object"
    ? saved.availability
    : { available: true, code: "ready", reason: "" };
  const available = availability.available !== false;
  const objective = config.objective || {};
  const budget = config.budget || {};
  const execution = config.execution || {};
  const evidence = config.evidence || {};
  const reproducibility = config.reproducibility || {};
  const environment = catalogEntryByRef("environment", saved.environment_ref)
    || catalogReference("environment", saved.environment_ref, config.environmentConfig);
  const method = catalogEntryByRef("method", saved.method_ref)
    || catalogReference("method", saved.method_ref, config.methodConfig);
  return {
    id: `draft-${saved.draft_id}`,
    title: saved.title || config.name || "Saved Study draft",
    source: "Saved draft",
    status: !available
      ? "unavailable"
      : saved.validation && saved.validation.valid === false
      ? "review"
      : "saved",
    study: null,
    validation: null,
    environment,
    method,
    name: config.name || saved.title || "",
    description: config.description || "",
    tags: config.tags || [],
    metric: objective.metric || "",
    direction: objective.direction || "maximize",
    aggregation: objective.aggregation || "mean",
    secondaryMetrics: objective.secondaryMetrics || [],
    maxTrials: budget.maxTrials || "",
    maxWallClockSeconds: budget.maxWallClockSeconds || "",
    maxFailures: budget.maxFailures || "",
    parallelism: execution.parallelism || "",
    timeoutSeconds: execution.timeoutSeconds || "",
    methodRequestTimeoutSeconds: 10,
    maxRetries: (execution.retry && execution.retry.maxRetries) ?? "",
    evidenceLevel: evidence.level || "standard",
    seed: reproducibility.seed ?? "",
    checks: [],
    yaml: saved.yaml || "",
    draft: {
      draft_id: saved.draft_id,
      draft_revision: saved.draft_revision,
      workspace_id: saved.workspace_id,
      workspace_revision: saved.workspace_revision,
      study_relative_path: saved.study_relative_path,
      validation: saved.validation || null,
      saved_as_draft: true,
      dirty: false,
      available,
      availabilityCode: availability.code || (available ? "ready" : "unavailable"),
      unavailableReason: availability.reason || "",
    },
  };
}

function buildPlans() {
  const plans = [];
  for (const study of state.catalog.studies || []) {
    const summary = study.summary || {};
    const objective = summary.objective || {};
    const budget = summary.budget || {};
    const execution = summary.execution || {};
    const evidence = summary.evidence || {};
    const reproducibility = summary.reproducibility || {};
    const environmentRef = summary.environmentRef || summary.environment_ref || null;
    const methodRef = summary.methodRef || summary.method_ref || null;
    const environment = catalogEntryByRef("environment", environmentRef)
      || catalogReference("environment", environmentRef, summary.environment);
    const method = catalogEntryByRef("method", methodRef)
      || catalogReference("method", methodRef, summary.method);
    plans.push({
      id: `saved-${study.uid}`,
      title: study.label,
      source: study.qualified_id || study.catalog_key || study.id || study.label,
      status: "saved",
      study,
      validation: study.validation || null,
      environment,
      method,
      name: summary.name || study.label || "",
      description: summary.description || "",
      tags: summary.tags || [],
      metric: objective.metric || "",
      direction: objective.direction || "",
      aggregation: objective.aggregation || "mean",
      secondaryMetrics: objective.secondaryMetrics || [],
      maxTrials: budget.maxTrials || "",
      maxWallClockSeconds: budget.maxWallClockSeconds || "",
      maxFailures: budget.maxFailures || "",
      parallelism: execution.parallelism || "",
      timeoutSeconds: execution.timeoutSeconds || "",
      methodRequestTimeoutSeconds: 10,
      maxRetries: (execution.retry && execution.retry.maxRetries) ?? "",
      evidenceLevel: evidence.level || "",
      seed: reproducibility.seed ?? "",
      checks: [],
      yaml: study.yaml || `# Saved study\n# ${study.qualified_id || study.catalog_key || study.id || study.label}\n`,
      draft: null,
    });
  }
  for (const saved of state.studyDrafts || []) {
    plans.push(savedStudyDraftPlan(saved));
  }
  return plans;
}

function renderWorkspace() {
  renderActiveInterfaceIndicator();
  const allWorkspaces = orderedWorkspaceSessions();
  const session = currentSession();
  els.sessionCount.textContent = String(allWorkspaces.length);
  els.sessionCount.setAttribute(
    "aria-label",
    `${allWorkspaces.length} Workspace${allWorkspaces.length === 1 ? "" : "s"}`,
  );
  els.sessionCount.title = `${allWorkspaces.length} editable Workspace${allWorkspaces.length === 1 ? "" : "s"}`;
  els.sessionList.innerHTML = allWorkspaces.map(sessionCard).join("") || emptyInline("No workspaces yet.");
  document.querySelectorAll("[data-session-id]").forEach((button) => {
    button.addEventListener("click", () => selectSession(button.dataset.sessionId));
  });
  document.querySelectorAll("[data-close-workspace-id]").forEach((button) => {
    button.addEventListener("click", () => closeWorkspaceFromCurrentSession(button.dataset.closeWorkspaceId));
  });
  document.querySelectorAll("[data-attach-workspace-id]").forEach((button) => {
    button.addEventListener("click", () => attachWorkspaceAndRender(button.dataset.attachWorkspaceId));
  });
  document.querySelectorAll("[data-delete-workspace-id]").forEach((button) => {
    button.addEventListener("click", () => requestWorkspaceDelete(button.dataset.deleteWorkspaceId));
  });
  document.querySelectorAll("[data-workspace-action]").forEach((button) => {
    button.addEventListener("click", () => runWorkspaceAction(button.dataset.workspaceAction));
  });
  if (!session) {
    renderEmptyWorkspace();
    return;
  }
  renderCodeServerCard(session);
  els.sessionTitle.textContent = session.title;
  els.sessionPath.textContent = session.path;
  els.sessionStatus.textContent = session.status;
  els.sessionStatus.className = `status-pill ${statusClass(session.status)}`;
  if (els.workspaceContextNotice) {
    const catalogSourceView = isCatalogSourceView(session);
    const notice = workspaceNoticeForCurrentContext(session);
    els.workspaceContextNotice.hidden = !catalogSourceView && !notice;
    els.workspaceContextNotice.classList.toggle(
      "workspace-context-notice-error",
      Boolean(notice && notice.error),
    );
    els.workspaceContextNotice.classList.toggle(
      "catalog-inspector-notice",
      catalogSourceView && !notice,
    );
    els.workspaceContextNotice.innerHTML = notice ? `
        <strong>${escapeHtml(notice.title)}</strong>
        <span>${escapeHtml(notice.body)}</span>
      ` : catalogSourceView ? `
        <span class="catalog-inspector-notice-copy">
          <strong>Read-only Catalog item</strong>
          <span>Exact published version · not editable or listed in Workspaces</span>
        </span>
        <span class="catalog-inspector-notice-actions">
          <button class="ghost-button catalog-inspector-back" type="button" title="Return to the Catalog item details">Back to item</button>
          <button class="ghost-button catalog-inspector-edit" type="button">Edit in Workspace</button>
        </span>
      ` : "";
    const catalogComponent = catalogSourceView ? catalogSourceComponent(session) : null;
    const backButton = els.workspaceContextNotice.querySelector(".catalog-inspector-back");
    if (backButton) backButton.addEventListener("click", () => {
      setView("catalog");
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    });
    const editButton = els.workspaceContextNotice.querySelector(".catalog-inspector-edit");
    if (editButton) {
      editButton.disabled = !catalogComponent;
      editButton.addEventListener("click", () => {
        if (catalogComponent) openCatalogEditableWorkspace(catalogComponent);
      });
    }
  }
  renderWorkspaceWorkbenchToolbar(session);
  els.sessionSummary.innerHTML = (isCatalogSourceView(session) ? [
    ["Access", "read-only"],
    ["Version", "published Catalog version"],
    ["Source", shortPath(session.target)],
  ] : [
    ["Storage", workspaceStorageLabel(session)],
    ["Catalog", workspaceCatalogStatus(session)],
    ["Assistant", workspaceAssistantAccessLabel(session)],
    ["Interface", session.interface && (session.interface.profiles || []).length ? "available" : "not declared"],
  ]).filter(Boolean).map(summaryCell).join("");
  els.sessionFiles.innerHTML = Object.entries(session.files).map(([key, file]) => `
    <button class="file-tree-item ${key === state.selectedFileKey ? "active" : ""}" data-file-key="${escapeHtml(key)}" type="button">${escapeHtml(file.label)}</button>
  `).join("");
  document.querySelectorAll("[data-file-key]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedFileKey = button.dataset.fileKey;
      renderWorkspace();
    });
  });
  els.sessionContext.innerHTML = session.context.map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join("");
  els.sessionTools.innerHTML = session.tools.map(capabilityItem).join("");
  els.sessionWorkspaceActions.innerHTML = `
    <button class="file-tree-item open-session-code" type="button">${isCatalogSourceView(session) ? "View source in Code Server" : "Open folder in Code Server"}</button>
    <div class="path-text">${escapeHtml(shortPath(session.codeFolder || session.path))}</div>
  `;
  els.sessionWorkspaceActions.querySelector(".open-session-code").addEventListener("click", openCodeServerEmbedded);
  renderSessionEditor(session);
  renderWorkbenchMode();
  renderPreviewWorkbench();
  renderAssistant();
  renderSessionBottom();
  maybeAutoOpenCodeWorkspace(session);
}

function workspaceNoticeForCurrentContext(session) {
  const notice = state.workspaceNotice;
  if (!notice || !session || notice.workspaceId !== session.id) return null;
  if (
    notice.assistantSessionId
    && notice.assistantSessionId !== state.selectedAgentSessionId
  ) {
    state.workspaceNotice = null;
    return null;
  }
  return notice;
}

async function runWorkspaceAction(action) {
  if (action === "register") {
    await openRegistrationMenu();
    return;
  }
  if (action === "open-ide") {
    await openCodeServerFull();
  }
}

function renderAssistantSessionList() {
  if (!els.assistantSessionCards) return;
  els.assistantSessionCards.innerHTML = state.agentSessions.map(agentSessionCard).join("");
  document.querySelectorAll("#assistantSessionCards [data-agent-session-id]").forEach((button) => {
    button.addEventListener("click", () => selectAgentSession(button.dataset.agentSessionId));
  });
}

async function openRegistrationMenu() {
  const session = currentSession();
  if (!session) return;
  state.registrationDraft = buildRegistrationDraft(session, []);
  state.registrationNotice = null;
  state.workbenchMode = "setup";
  renderWorkbenchMode();
  if (!session.backendWorkspaceId) {
    state.registrationNotice = {
      error: true,
      title: "Publishing unavailable",
      body: "Save or reopen this Workspace before publishing it to Catalog.",
    };
    renderWorkspaceSetup();
    return;
  }
  try {
    const payload = await postJson(`/api/workspaces/${encodeURIComponent(session.backendWorkspaceId)}/package-plans`, {});
    state.registrationDraft = buildRegistrationDraftFromPackagePlan(session, payload.package_plan || {});
    renderWorkspaceSetup();
  } catch (error) {
    state.registrationNotice = {
      error: true,
      title: "Publish could not be opened",
      body: String(error.message || error),
    };
    renderWorkspaceSetup();
  }
}

function buildRegistrationDraft(session, discoveredConfigs = null) {
  const configs = (discoveredConfigs || [])
    .map((config) => ({
      key: config.relative_path || config.path,
      label: config.relative_path || config.path,
      kind: config.kind,
      id: config.id || config.label,
      selected: true,
      validation: "not checked",
      backendPath: config.relative_path || config.path,
      discoveredValid: Boolean(config.valid),
    }));
  const registeredEntries = session.registeredEntries || [];
  const alreadyRegistered = registeredEntries.length > 0 && configs.every((item) => item.validation === "read-only source");
  return {
    workspaceId: session.id,
    backendWorkspaceId: session.backendWorkspaceId || "",
    workspaceTitle: session.title,
    status: alreadyRegistered ? "applied" : configs.length ? "draft" : "needs-config",
    configs,
    registeredEntries,
    setupRole: "reference",
    resourceId: slug(session.title || session.id || "resource"),
    resourceDescription: "",
    note: alreadyRegistered
      ? "This Workspace already has a published Catalog version. Keep editing here and publish another version when it is ready."
      : configs.length
      ? "Select one or more configurations, check them, then publish the selected Catalog items."
      : "No Environment or Method configuration was found. Add one in Code, or publish this Workspace as a reusable Resource.",
  };
}

function buildRegistrationDraftFromPackagePlan(session, plan) {
  const targets = [
    ...(plan.components || []),
    ...(plan.resources || []),
    ...(plan.studies || []),
  ];
  const validationEntries = (plan.validation && plan.validation.entries || []);
  const configs = targets.map((target) => {
    const path = target.config_path || target.registered_config_path || target.id;
    const validation = validationEntries.find((entry) => entry.id === target.id || entry.config === target.kind && path && entry.path && entry.path.endsWith(path));
    const retainedExecution = validation && validation.capabilities && validation.capabilities.retained_execution;
    return {
      key: target.target_id || path,
      label: path,
      kind: target.kind,
      id: target.id || target.label,
      selected: true,
      validation: validation
        ? (!validation.valid
          ? "invalid"
          : retainedExecution && !retainedExecution.eligible
          ? retainedExecution.smoke_eligible === false ? "Run test unavailable" : "requires Test"
          : "valid")
        : "not checked",
      backendPath: path,
      discoveredValid: target.validation ? Boolean(target.validation.valid) : true,
      target,
    };
  });
  return {
    workspaceId: session.id,
    backendWorkspaceId: session.backendWorkspaceId || "",
    workspaceTitle: session.title,
    status: plan.status || "draft",
    packagePlanId: plan.id,
    packagePlan: plan,
    classification: plan.classification || "not-yet-classifiable",
    readiness: plan.readiness || "draft",
    configs,
    registeredEntries: session.registeredEntries || [],
    setupRole: "reference",
    resourceId: slug(session.title || session.id || "resource"),
    resourceDescription: "",
    note: `Publish status: ${plan.readiness || "draft"}.`,
  };
}

function packagePlanContextSummary(plan) {
  return {
    id: plan.id || "",
    package_id: plan.package_id || "",
    classification: plan.classification || "",
    readiness: plan.readiness || "",
    destination: plan.destination || "",
    source_root: plan.source_root || "",
    validation: plan.validation ? {
      valid: Boolean(plan.validation.valid),
      errors: plan.validation.errors || [],
      capabilities: plan.validation.capabilities || {},
      entries: (plan.validation.entries || []).map((entry) => ({
        config: entry.config,
        id: entry.id,
        valid: Boolean(entry.valid),
        errors: entry.errors || [],
        warnings: entry.warnings || [],
        capabilities: entry.capabilities || {},
      })),
    } : null,
    smoke: plan.smoke || {},
    components: (plan.components || []).map(packagePlanTargetContext),
    resources: (plan.resources || []).map(packagePlanTargetContext),
    studies: (plan.studies || []).map((study) => ({
      id: study.id,
      path: study.config_path,
      registered_config_path: study.registered_config_path,
      smoke: Boolean(study.smoke),
    })),
  };
}

function packagePlanTargetContext(target) {
  return {
    kind: target.kind,
    id: target.id,
    config_path: target.config_path,
    component_root: target.component_root,
    include: target.include || [],
    exclude: target.exclude || [],
    source_hints: target.source_hints || [],
    path_rewrites: target.path_rewrites || [],
    runtime: componentExecutionSummary(target.raw_config || {}),
  };
}

function registrationMenuHtml() {
  const session = currentSession();
  const draft = state.registrationDraft || (session ? buildRegistrationDraft(session) : null);
  if (!draft) return emptyState("Select a Workspace before publishing to Catalog.");
  state.registrationDraft = draft;
  const configs = draft.configs || [];
  const plan = draft.packagePlan || null;
  const configuredWholePackage = plan && plan.publication_scope === "configured-whole-package";
  const notice = state.registrationNotice;
  if (draft.status === "applied") {
    return publishedRegistrationHtml(draft, plan, notice);
  }
  const testNotRequired = packagePlanTestNotRequired(plan);
  return `
    <div class="registration-panel">
      <div class="registration-summary">
        <div>
          <span class="mini-label">Publish to Catalog</span>
          <h2>${escapeHtml(draft.workspaceTitle)}</h2>
        </div>
        <p>Check the current files, then publish one reusable, immutable version. You can keep editing this Workspace afterward.</p>
        ${plan ? `<div class="registration-summary-tags"><span class="tag">${escapeHtml(registrationCatalogName(plan))}</span><span class="tag">${escapeHtml(registrationKindLabel(plan))}</span></div>` : ""}
        ${registrationNoticeHtml(notice)}
      </div>
      <ol class="registration-steps" aria-label="Publishing progress">
        ${registrationStep("1", "Configure", configs.length ? `${packagePlanTargetCount(plan)} reusable Catalog item${packagePlanTargetCount(plan) === 1 ? "" : "s"} configured` : "Choose what this Workspace should publish", configs.length ? "ready" : "review")}
        ${registrationStep("2", "Check files", packageValidationSummary(plan, configs), packagePlanCheckPassed(plan) ? "passed" : "review")}
        ${registrationStep("3", "Test", packageSmokeSummary(plan), registrationTestStepStatus(plan), testNotRequired ? "Not required" : "")}
        ${registrationStep("4", "Publish checked version", planCanApply(draft) ? "Ready for confirmation" : registrationBlockedSummary(plan), planCanApply(draft) ? "ready" : "review")}
      </ol>
      ${packagePlanDetailsHtml(plan)}
      ${configuredWholePackage
        ? `<section class="registration-targets registration-scope"><h3>Files to publish</h3><p><strong>Whole configured folder</strong></p><p>Authored files are checked and published together, so this linked folder remains the one place you edit them. Local dependencies and caches such as <code>node_modules</code> and <code>.venv</code> stay out.</p></section>`
        : `<div class="registration-targets">${configs.map(registrationTarget).join("") || emptyInline("No config files yet.")}</div>`}
      ${resourceRegistrationHtml(draft)}
      ${registrationActionHierarchyHtml(draft)}
    </div>
  `;
}

function registrationNoticeHtml(notice) {
  if (!notice) return "";
  return `<div class="registration-notice ${notice.error ? "error" : "ready"}" role="${notice.error ? "alert" : "status"}"><strong>${escapeHtml(notice.title || "Publishing update")}</strong><p>${escapeHtml(notice.body || "")}</p></div>`;
}

function publishedRegistrationHtml(draft, plan, notice) {
  const artifact = plan && plan.artifact || {};
  const registeredEntries = Array.isArray(draft && draft.registeredEntries) ? draft.registeredEntries : [];
  const itemCount = plan ? packagePlanTargetCount(plan) : registeredEntries.length;
  const publicationTitle = plan ? registrationCatalogName(plan) : draft.workspaceTitle || "Published version";
  const fileCount = Number(artifact.file_count || 0);
  const logicalBytes = Number(artifact.logical_bytes || 0);
  return `
    <div class="registration-panel registration-published">
      <section class="publication-success" aria-labelledby="publicationSuccessTitle">
        <div class="publication-success-mark" aria-hidden="true">✓</div>
        <div>
          <span class="mini-label">Published to Catalog</span>
          <h2 id="publicationSuccessTitle">${escapeHtml(publicationTitle)}</h2>
          <p>This immutable Catalog version is ready to reuse. Your editable Workspace remains separate.</p>
        </div>
        <button class="primary-button registration-open-catalog" type="button">View in Catalog</button>
      </section>
      ${registrationNoticeHtml(notice)}
      <dl class="publication-facts">
        <div><dt>Contents</dt><dd>${escapeHtml(`${itemCount} Catalog item${itemCount === 1 ? "" : "s"}`)}</dd></div>
        <div><dt>Files</dt><dd>${escapeHtml(fileCount ? `${fileCount} · ${formatBytes(logicalBytes)}` : "Published version")}</dd></div>
        <div><dt>Workspace</dt><dd>Still editable</dd></div>
      </dl>
      <details class="publication-details">
        <summary>Publication details</summary>
        <div class="publication-detail-grid">
          <section>
            <h3>Source</h3>
            <p>Published from <strong>${escapeHtml(draft.workspaceTitle)}</strong>. Later edits do not change this Catalog version.</p>
          </section>
          <section>
            <h3>Validation</h3>
            <p>${plan
              ? `${escapeHtml(packageValidationSummary(plan, draft.configs || []))}. ${escapeHtml(packageSmokeSummary(plan))}.`
              : "This version was already checked when it was published."}</p>
          </section>
          <section>
            <h3>Scope</h3>
            <p>${plan && plan.publication_scope === "configured-whole-package"
              ? "The connected folder's authored files were published as one package; machine-local dependencies and caches were omitted."
              : `${escapeHtml(itemCount)} reusable item${itemCount === 1 ? "" : "s"} were published.`}</p>
          </section>
        </div>
        <div class="registration-more-actions">
          <button class="ghost-button registration-discover" type="button">Detect current files again</button>
          <button class="ghost-button registration-validate" type="button">Check current files again</button>
        </div>
        <p class="publication-maintenance-note">These actions prepare a possible future version. They do not change the version already in Catalog.</p>
      </details>
    </div>
  `;
}

function renderWorkspaceSetup() {
  if (!els.workspaceSetupContent) return;
  const session = currentSession();
  if (!session) {
    els.workspaceSetupContent.innerHTML = emptyState("Select a Workspace to publish it to Catalog.");
    return;
  }
  els.workspaceSetupContent.innerHTML = registrationMenuHtml();
  bindRegistrationMenu();
}

function renderRegistrationExperience() {
  if (state.workbenchMode === "setup") renderWorkspaceSetup();
  if (state.assistantOpen && state.assistantMode === "registration") renderAssistant();
}

function packagePlanTargetCount(plan) {
  if (!plan) return 0;
  return (plan.components || []).length + (plan.resources || []).length + (plan.studies || []).length;
}

function packagePlanIncludeCount(plan) {
  if (!plan) return 0;
  return [...(plan.components || []), ...(plan.resources || [])].reduce((count, item) => count + (item.include || []).length + (item.source_hints || []).length, 0);
}

function packageValidationSummary(plan, configs) {
  if (plan && plan.validation) {
    const entries = plan.validation.entries || [];
    const invalid = entries.filter((entry) => !entry.valid).length + (plan.validation.errors || []).length;
    const retainedExecution = packageRetainedExecutionCapability(plan);
    if (
      plan.validation.valid
      && retainedExecution
      && !retainedExecution.eligible
      && retainedExecution.smoke_eligible !== false
    ) {
      return "Static checks passed; run Test to verify executable behavior";
    }
    if (plan.validation.valid && retainedExecution && !retainedExecution.eligible) {
      return `Check passed, but Study execution is unavailable: ${retainedExecution.reason || retainedExecution.code || "unsupported"}`;
    }
    if (plan.validation.valid && plan.publication_scope === "configured-whole-package") {
      return "Whole folder passed static checks";
    }
    if (plan.validation.valid) return `${entries.length} entries passed schema, source, and setup checks`;
    return invalid ? `${invalid} blocker${invalid === 1 ? "" : "s"} found` : "Validation did not pass";
  }
  return validationSummary(configs);
}

function packageSmokeSummary(plan) {
  if (!plan) return "Prepare package first";
  if (packagePlanTestNotRequired(plan)) return "Not needed; Check is static and does not execute Workspace code";
  if (plan.smoke && plan.smoke.valid) return "Test passed";
  if (plan.smoke && plan.smoke.errors && plan.smoke.errors.length) return plan.smoke.errors[0];
  const retainedExecution = packageRetainedExecutionCapability(plan);
  if (plan.classification === "environment-plus-method" && retainedExecution && !retainedExecution.eligible) {
    return retainedExecution.reason || "The Run test is unavailable";
  }
  if (!(plan.studies || []).length) return "No study available";
  return "Not run yet";
}

function packagePlanTestNotRequired(plan) {
  return Boolean(plan && plan.publication_scope === "configured-whole-package");
}

function packageRetainedExecutionCapability(plan) {
  return plan && plan.validation && plan.validation.capabilities
    ? plan.validation.capabilities.retained_execution || null
    : null;
}

function packagePlanCanSmoke(plan) {
  if (packagePlanTestNotRequired(plan)) return false;
  if (!plan || !(plan.studies || []).length) return false;
  const retainedExecution = packageRetainedExecutionCapability(plan);
  return plan.classification !== "environment-plus-method"
    || !retainedExecution
    || retainedExecution.smoke_eligible !== false;
}

function packagePlanCheckPassed(plan) {
  return Boolean(plan && plan.validation && plan.validation.valid);
}

function packagePlanRequiresTest(plan) {
  return Boolean(
    plan
    && !packagePlanTestNotRequired(plan)
    && plan.classification === "environment-plus-method",
  );
}

function registrationTestStepStatus(plan) {
  if (packagePlanTestNotRequired(plan) || plan && plan.smoke && plan.smoke.valid) return "passed";
  if (!plan || !packagePlanCheckPassed(plan)) return "review";
  return packagePlanRequiresTest(plan) ? "review" : "optional";
}

function registrationBlockedSummary(plan) {
  if (!plan || !packagePlanCheckPassed(plan)) return "Check the files first";
  if (packagePlanRequiresTest(plan) && !(plan.smoke && plan.smoke.valid)) {
    return packagePlanCanSmoke(plan) ? "Run the required test" : "Required test is unavailable";
  }
  return "Not ready to publish";
}

function registrationKindLabel(plan) {
  if (!plan) return "Workspace";
  const kinds = [
    ...(plan.components || []).map((item) => item.kind),
    ...(plan.resources || []).map((item) => item.kind),
    ...(plan.studies || []).map(() => "study"),
  ].filter((value, index, values) => value && values.indexOf(value) === index);
  return kinds.length ? kinds.map(fieldLabel).join(" + ") : "Workspace";
}

function registrationCatalogName(plan) {
  if (!plan) return "Unconfigured Workspace";
  const ids = [
    ...(plan.components || []).map((item) => item.id),
    ...(plan.resources || []).map((item) => item.id),
    ...(plan.studies || []).map((item) => item.id),
  ].filter((value, index, values) => value && values.indexOf(value) === index);
  if (!ids.length) return plan.package_id || "Unconfigured Workspace";
  if (ids.length === 1) return ids[0];
  return `${ids[0]} + ${ids.length - 1} more`;
}

function registrationActionHierarchyHtml(draft) {
  const configs = draft && draft.configs || [];
  const plan = draft && draft.packagePlan || null;
  const pendingAction = state.registrationActionPending;
  const noticeTitle = String(state.registrationNotice && state.registrationNotice.title || "");
  const retryCheck = /check .*failed|check found problems/i.test(noticeTitle);
  const retryTest = /test failed/i.test(noticeTitle);
  const checked = packagePlanCheckPassed(plan);
  const testPassed = Boolean(plan && plan.smoke && plan.smoke.valid);
  const testRequired = packagePlanRequiresTest(plan);
  const canTest = packagePlanCanSmoke(plan);
  let primary = "";
  let secondary = "";
  if (pendingAction === "check") {
    primary = '<button class="primary-button registration-validate" type="button" disabled>Checking files…</button>';
  } else if (pendingAction === "test") {
    primary = '<button class="primary-button registration-smoke" type="button" disabled>Testing…</button>';
  } else if (draft && draft.status === "applied") {
    primary = '<button class="primary-button registration-open-catalog" type="button">View in Catalog</button>';
  } else if (configs.length && !checked) {
    primary = `<button class="primary-button registration-validate" type="button">${retryCheck ? "Try checking again" : "Check files"}</button>`;
  } else if (checked && testRequired && !testPassed && canTest) {
    primary = `<button class="primary-button registration-smoke" type="button">${retryTest ? "Try required test again" : "Run required test"}</button>`;
  } else if (planCanApply(draft)) {
    primary = '<button class="primary-button registration-apply" type="button">Publish checked version</button>';
    if (canTest && !testPassed) {
      secondary = `<button class="ghost-button registration-smoke" type="button">${retryTest ? "Try optional test again" : "Run optional test"}</button>`;
    }
  }
  const secondaryActionsDisabled = pendingAction ? "disabled" : "";
  return `
    ${primary || secondary ? `<div class="registration-actions">${primary}${secondary}</div>` : ""}
    <details class="registration-more">
      <summary>More</summary>
      <div class="registration-more-actions">
        <button class="ghost-button registration-discover" type="button" ${secondaryActionsDisabled}>Find Catalog items again</button>
        ${checked ? `<button class="ghost-button registration-validate" type="button" ${secondaryActionsDisabled}>${pendingAction === "check" ? "Checking files…" : retryCheck ? "Try checking again" : "Check files again"}</button>` : ""}
      </div>
      <p>Detection and re-checking update this publishing plan without publishing anything.</p>
    </details>
  `;
}

function planCanApply(draft) {
  const plan = draft && draft.packagePlan;
  if (!plan || !(plan.validation && plan.validation.valid)) return false;
  if (!packagePlanTestNotRequired(plan) && plan.classification === "environment-plus-method" && !(plan.smoke && plan.smoke.valid)) return false;
  return true;
}

function packagePlanDetailsHtml(plan) {
  if (!plan) return "";
  if (plan.publication_scope === "configured-whole-package") {
    const artifact = plan.artifact || {};
    const checkedSummary = artifact.content_ref
      ? `${artifact.file_count || 0} files · ${formatBytes(Number(artifact.logical_bytes || 0))}`
      : "Run Check to capture the exact current files";
    return `
      <div class="registration-plan">
        <div class="config-section-title">
          <div><span class="mini-label">Checked Workspace</span><strong>${escapeHtml(plan.package_id || "Workspace contents")}</strong></div>
          <small>${escapeHtml(checkedSummary)}</small>
        </div>
        <p>Authored files in this linked folder will be published together. Machine-local dependencies and caches are omitted, no second editable Workspace is created, and Workspace code is not executed during Check.</p>
        ${plan.validation ? packagePlanValidationHtml(plan.validation) : ""}
      </div>
    `;
  }
  const targets = [...(plan.components || []), ...(plan.resources || [])];
  const validationEntries = plan.validation && plan.validation.entries || [];
  return `
    <div class="registration-plan">
      <div class="config-section-title">
        <div>
          <span class="mini-label">Checked Workspace</span>
          <strong>${escapeHtml(plan.package_id || "local_package")}</strong>
        </div>
        <small>${escapeHtml(packagePlanTargetCount(plan))} reusable item${packagePlanTargetCount(plan) === 1 ? "" : "s"}</small>
      </div>
      <details class="registration-advanced">
        <summary>Advanced file selection</summary>
        ${targets.map((target) => packagePlanTargetHtml(target, validationEntries)).join("") || emptyInline("No reusable items detected yet.")}
      </details>
      ${(plan.studies || []).length ? `
        <div class="registration-plan-block">
          <strong>Studies</strong>
          ${(plan.studies || []).map((study) => `
            <label class="registration-target compact">
              <input type="checkbox" data-package-plan-study-smoke="${escapeHtml(study.target_id || study.id)}" ${study.smoke ? "checked" : ""} />
              <span><strong>${escapeHtml(study.registered_config_path || study.config_path || study.id)}</strong><small>${study.smoke ? "selected for Test" : "not selected for Test"}</small></span>
            </label>
          `).join("")}
        </div>
      ` : ""}
      ${plan.validation ? packagePlanValidationHtml(plan.validation) : ""}
      ${plan.smoke && (plan.smoke.valid || plan.smoke.errors) ? packagePlanSmokeHtml(plan.smoke) : ""}
    </div>
  `;
}

function packagePlanTargetHtml(target, validationEntries) {
  const validation = validationEntries.find((entry) => entry.id === target.id && entry.config === target.kind);
  const retainedExecution = validation && validation.capabilities && validation.capabilities.retained_execution;
  return `
    <div class="registration-plan-block">
      <div class="config-section-title">
        <div><strong>${escapeHtml(target.id || target.label || target.kind)}</strong><small>${escapeHtml(target.kind || "")} -> ${escapeHtml(target.component_root || "")}</small></div>
        ${statusPill(validation ? (!validation.valid ? "failed" : retainedExecution && !retainedExecution.eligible ? "review" : "passed") : "review")}
      </div>
      ${targetSetupSummaryHtml(target)}
      <label class="control-field">
        <span>Include paths</span>
        <textarea data-package-plan-list="include" data-package-plan-target="${escapeHtml(target.target_id || target.id)}">${escapeHtml((target.include || []).join("\\n"))}</textarea>
      </label>
      <label class="control-field">
        <span>Exclude paths</span>
        <textarea data-package-plan-list="exclude" data-package-plan-target="${escapeHtml(target.target_id || target.id)}">${escapeHtml((target.exclude || []).join("\\n"))}</textarea>
      </label>
      <label class="control-field">
        <span>Source hints</span>
        <textarea data-package-plan-list="source_hints" data-package-plan-target="${escapeHtml(target.target_id || target.id)}">${escapeHtml((target.source_hints || []).map((hint) => typeof hint === "string" ? hint : hint.path || "").join("\\n"))}</textarea>
      </label>
      <label class="control-field">
        <span>Path rewrites JSON</span>
        <textarea data-package-plan-json="path_rewrites" data-package-plan-target="${escapeHtml(target.target_id || target.id)}">${escapeHtml(JSON.stringify(target.path_rewrites || [], null, 2))}</textarea>
      </label>
      ${validation && validation.errors && validation.errors.length ? `<p class="error-text">${escapeHtml(validation.errors.join(" "))}</p>` : ""}
      ${retainedExecution && !retainedExecution.eligible
        ? retainedExecution.smoke_eligible === false
          ? `<p class="error-text">Run test unavailable: ${escapeHtml(retainedExecution.reason || retainedExecution.code || "unsupported")}</p>`
          : `<p>Run Test to verify executable behavior: ${escapeHtml(retainedExecution.reason || retainedExecution.code || "method callable not executed")}</p>`
        : ""}
    </div>
  `;
}

function targetSetupSummaryHtml(target) {
  const raw = target.raw_config || {};
  const runtime = raw.runtime || {};
  const profiles = authoredInterfaceProfiles(raw.interface);
  const setup = runtime.setup || profiles.some((profile) => profile.runtime && profile.runtime.setup);
  const preparedPython = Boolean(
    runtime.setup
    && runtime.setup.cache === "prepared"
    && Array.isArray(runtime.setup.steps)
    && runtime.setup.steps.length === 1
    && runtime.setup.steps[0]
    && runtime.setup.steps[0].uses === "python-venv",
  );
  const envFromHost = [
    ...(runtime.envFromHost || []),
    ...((runtime.setup && runtime.setup.envFromHost) || []),
    ...profiles.flatMap((profile) => [
      ...(profile.grants && profile.grants.envFromHost || []),
      ...(profile.grants && profile.grants.secretsFromHost || []),
    ]),
  ];
  if (!setup && !envFromHost.length) return "";
  if (preparedPython) {
    return `<p><small>Prepared Python dependencies declared. Check verifies the declaration and referenced lock-file paths; Test verifies the supplied packages, prepares them, and runs the Study with its normal runtime.</small></p>`;
  }
  return `<p><small>${setup ? "setup declared" : "no setup"}${envFromHost.length ? `; env: ${escapeHtml(envFromHost.join(", "))}` : ""}</small></p>`;
}

function packagePlanValidationHtml(validation) {
  const entries = validation.entries || [];
  const retainedExecution = validation.capabilities && validation.capabilities.retained_execution;
  const configuredStatic = validation.test_policy === "static-only";
  return `
    <div class="registration-plan-block">
      <strong>Validation</strong>
      <p>${escapeHtml(validation.valid
        ? configuredStatic
          ? "The exact authored files passed static checks; machine-local dependencies and caches were omitted, and Workspace code was not executed."
          : "Schema, source paths, setup declarations, and available static checks passed; Workspace code was not executed."
        : "Package validation found blockers.")}</p>
      ${entries.map((entry) => `<p><small>${escapeHtml(entry.config)} ${escapeHtml(entry.id)}: ${escapeHtml(entry.valid ? "passed" : (entry.errors || []).join(" "))}</small></p>`).join("")}
      ${retainedExecution && !retainedExecution.eligible && (retainedExecution.methods || []).length
        ? retainedExecution.smoke_eligible === false
          ? `<p class="error-text">Run test unavailable: ${escapeHtml(retainedExecution.reason || retainedExecution.code || "unsupported")}</p>`
          : `<p>Run Test to verify executable behavior: ${escapeHtml(retainedExecution.reason || retainedExecution.code || "method callable not executed")}</p>`
        : ""}
      ${(validation.errors || []).length ? `<p class="error-text">${escapeHtml(validation.errors.join(" "))}</p>` : ""}
    </div>
  `;
}

function packagePlanSmokeHtml(smoke) {
  return `
    <div class="registration-plan-block">
      <strong>Test</strong>
      <p>${escapeHtml(smoke.valid ? "Test passed in the Study runtime." : "Test failed in the Study runtime.")}</p>
      ${smoke.study ? `<p><small>${escapeHtml(smoke.study)}</small></p>` : ""}
      ${smoke.errors && smoke.errors.length ? `<p class="error-text">${escapeHtml(smoke.errors.join(" "))}</p>` : ""}
    </div>
  `;
}

function resourceRegistrationHtml(draft) {
  if (!draft || draft.status === "applied") return "";
  if ((draft.configs || []).length) return "";
  const role = draft.setupRole || "reference";
  const roleNeedsCode = role === "environment" || role === "method";
  const starterGuidance = role === "environment"
    ? "Configure creates optpilot_configs/environment.template.yaml.disabled and optpilot_adapter.py. Review the Candidate inputs, make the adapter's metric_values names match metrics.keys, then rename the template to environment.yaml and choose Find Catalog items again."
    : "Configure creates optpilot_configs/method.template.yaml.disabled and optpilot_method.py. Implement the proposal contract, then rename the template to method.yaml and choose Find Catalog items again.";
  const curationBlocked = !currentAgentSession() || assistantIsBusy() || assistantIsAwaitingApproval();
  const assistantLabel = assistantSessionLabel();
  return `
    <div class="registration-resource">
      <div>
        <strong>What should this Workspace publish?</strong>
        <p>Choose a role. Simple reusable files get a complete config; Environment and Method starters deliberately require you to define their domain-specific contract in Code.</p>
      </div>
      <label class="control-field">
        <span>Catalog role</span>
        <select data-resource-registration-field="setupRole">
          <option value="environment" ${role === "environment" ? "selected" : ""}>Environment · evaluates Candidates</option>
          <option value="method" ${role === "method" ? "selected" : ""}>Method · proposes Candidates</option>
          <option value="generator" ${role === "generator" ? "selected" : ""}>Generator</option>
          <option value="viewer" ${role === "viewer" ? "selected" : ""}>Viewer</option>
          <option value="template" ${role === "template" ? "selected" : ""}>Template</option>
          <option value="reference" ${role === "reference" ? "selected" : ""}>Reference</option>
        </select>
      </label>
      <label class="control-field">
        <span>Catalog name</span>
        <input data-resource-registration-field="resourceId" type="text" value="${escapeHtml(draft.resourceId || "")}" />
      </label>
      <label class="control-field">
        <span>Description</span>
        <input data-resource-registration-field="resourceDescription" type="text" value="${escapeHtml(draft.resourceDescription || "")}" />
      </label>
      ${roleNeedsCode ? `<p class="registration-role-guidance">${escapeHtml(starterGuidance)}</p>` : ""}
      <button class="primary-button registration-resource-apply" type="button">${escapeHtml(roleNeedsCode ? `Create ${fieldLabel(role)} starter` : `Configure as ${fieldLabel(role)}`)}</button>
      <details class="registration-assistant-help">
        <summary>Want help configuring?</summary>
        <p>This makes the Workspace available to ${escapeHtml(assistantLabel)} and opens that Assistant conversation. You can also configure and publish without Assistant.</p>
        <button class="ghost-button registration-curate-assistant" type="button" ${curationBlocked ? "disabled" : ""} title="${curationBlocked ? "Assistant is unavailable or busy. You can continue with Configure." : `Make this Workspace available to ${escapeHtml(assistantLabel)} and ask for configuration help.`}">Ask in ${escapeHtml(assistantLabel)}</button>
      </details>
    </div>
  `;
}

function registrationStep(number, title, text, status, statusLabel = "") {
  return `
    <li class="registration-step">
      <span>${escapeHtml(number)}</span>
      <div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(text)}</p></div>
      <span class="status-pill ${statusClass(status || "review")}">${escapeHtml(statusLabel || status || "review")}</span>
    </li>
  `;
}

function registrationTarget(config) {
  const stateText = config.selected ? "selected" : "not selected";
  const validationText = config.validation === "not checked" ? "not validated" : config.validation;
  return `
    <label class="registration-target">
      <input type="checkbox" data-registration-target="${escapeHtml(config.key)}" ${config.selected ? "checked" : ""} />
      <span>
        <strong>${escapeHtml(config.label)}</strong>
        <small>${escapeHtml(config.kind)} - ${escapeHtml(config.id)} - ${escapeHtml(stateText)} - ${escapeHtml(validationText)}</small>
      </span>
    </label>
  `;
}

function validationSummary(configs) {
  if (!configs.length) return "No configs";
  if (configs.every((item) => item.validation === "read-only source")) return "Already published";
  const valid = configs.filter((item) => item.validation === "valid").length;
  return valid ? `${valid} schema-valid target${valid === 1 ? "" : "s"}` : "Not validated";
}

function setRegistrationNotice(title, body, error = false) {
  state.registrationNotice = { title, body, error: Boolean(error) };
  renderRegistrationExperience();
}

function openRegisteredCatalogResult(applied) {
  if (!applied || !applied.applied) return false;
  const entries = applied.workspace && (applied.workspace.registered_entries || applied.workspace.registeredEntries) || [];
  const component = entries.map((registered) => allComponents().find((item) => (
    item.kind === registered.kind
    && item.entry
    && item.entry.id === registered.id
    && (!registered.package_id || item.entry.package_id === registered.package_id)
  ))).find(Boolean);
  if (component) state.selectedComponentKey = component.key;
  setView("catalog");
  return true;
}

function registrationTestConfirmation(plan) {
  if (plan && plan.smoke && plan.smoke.valid) {
    return { label: "Passed", detail: "The supported test passed for these checked files." };
  }
  if (packagePlanTestNotRequired(plan)) {
    return { label: "Skipped", detail: "This linked folder uses static Check only; Workspace code was not executed." };
  }
  if (!packagePlanCanSmoke(plan)) {
    return { label: "Skipped", detail: "No supported test is available for this Catalog item." };
  }
  return { label: "Skipped", detail: "Test is optional for this component and was not run." };
}

function buildRegistrationConfirmation(draft) {
  const plan = draft && draft.packagePlan || {};
  const artifact = plan.artifact || {};
  const validation = plan.validation || {};
  const source = artifact.source || {};
  const checkedAt = artifact.sealed_at || plan.updated_at || "";
  return {
    workspaceId: draft.workspaceId,
    backendWorkspaceId: draft.backendWorkspaceId,
    packagePlanId: draft.packagePlanId,
    catalogName: registrationCatalogName(plan),
    catalogPackage: plan.package_id || "local_package",
    componentKind: registrationKindLabel(plan),
    checkedAt,
    checkResult: validation.valid
      ? `Passed · ${Number(artifact.file_count || 0)} file${Number(artifact.file_count || 0) === 1 ? "" : "s"}`
      : "Did not pass",
    testResult: registrationTestConfirmation(plan),
    sourceMatches: Boolean(validation.valid && artifact.content_ref),
    artifactRef: artifact.content_ref || "",
    packagePlanTechnicalId: plan.id || "",
    workspaceRevision: source.workspace_revision || null,
    submitting: false,
    error: "",
  };
}

function openRegistrationConfirmation(draft) {
  state.pendingRegistrationConfirmation = buildRegistrationConfirmation(draft);
  renderRegistrationConfirmation();
  window.requestAnimationFrame(() => {
    if (els.registrationConfirmationSubmitButton) els.registrationConfirmationSubmitButton.focus();
  });
}

function closeRegistrationConfirmation(options = {}) {
  state.pendingRegistrationConfirmation = null;
  if (els.registrationConfirmationModal) els.registrationConfirmationModal.hidden = true;
  if (options.restoreFocus !== false) {
    window.requestAnimationFrame(() => {
      const button = document.querySelector(".registration-apply");
      if (button) button.focus();
    });
  }
}

function renderRegistrationConfirmation() {
  const pending = state.pendingRegistrationConfirmation;
  if (!els.registrationConfirmationModal || !els.registrationConfirmationBody) return;
  if (!pending) {
    els.registrationConfirmationModal.hidden = true;
    return;
  }
  const checkedTime = formatRealmTime(pending.checkedAt) || "just now";
  const matchText = pending.sourceMatches
    ? "Current files match the checked version"
    : "Files need checking again";
  els.registrationConfirmationBody.innerHTML = `
    <section class="registration-confirmation-summary" aria-label="Checked publication summary">
      <div><span>Catalog name</span><strong>${escapeHtml(pending.catalogName)}</strong></div>
      <div><span>Component kind</span><strong>${escapeHtml(pending.componentKind)}</strong></div>
      <div><span>Checked version</span><strong>${escapeHtml(`From ${checkedTime}`)}</strong></div>
      <div><span>Check</span><strong>${escapeHtml(pending.checkResult)}</strong></div>
      <div><span>Test</span><strong>${escapeHtml(pending.testResult.label)}</strong><small>${escapeHtml(pending.testResult.detail)}</small></div>
      <div class="registration-source-match ${pending.sourceMatches ? "matches" : "changed"}"><span>Current files</span><strong>${escapeHtml(matchText)}</strong></div>
    </section>
    <p>Publishing creates an immutable Catalog version from these checked files. Your editable Workspace remains separate.</p>
    <details class="registration-confirmation-technical">
      <summary>More · technical details</summary>
      <dl>
        <div><dt>Checked artifact</dt><dd><code title="${escapeHtml(pending.artifactRef)}">${escapeHtml(shortDigest(pending.artifactRef))}</code></dd></div>
        <div><dt>Catalog package</dt><dd><code>${escapeHtml(pending.catalogPackage)}</code></dd></div>
        ${pending.workspaceRevision ? `<div><dt>Workspace revision</dt><dd>${escapeHtml(pending.workspaceRevision)}</dd></div>` : ""}
        <div><dt>Setup record</dt><dd><code title="${escapeHtml(pending.packagePlanTechnicalId)}">${escapeHtml(pending.packagePlanTechnicalId)}</code></dd></div>
      </dl>
    </details>
  `;
  if (els.registrationConfirmationError) {
    els.registrationConfirmationError.hidden = !pending.error;
    els.registrationConfirmationError.textContent = pending.error || "";
  }
  if (els.registrationConfirmationSubmitButton) {
    els.registrationConfirmationSubmitButton.disabled = pending.submitting || !pending.sourceMatches;
    els.registrationConfirmationSubmitButton.textContent = pending.submitting ? "Publishing…" : "Publish";
  }
  if (els.registrationConfirmationCancelButton) els.registrationConfirmationCancelButton.disabled = pending.submitting;
  if (els.registrationConfirmationCloseButton) els.registrationConfirmationCloseButton.disabled = pending.submitting;
  els.registrationConfirmationModal.hidden = false;
}

function handleRegistrationConfirmationKeydown(event) {
  const pending = state.pendingRegistrationConfirmation;
  if (!pending) return;
  if (event.key === "Escape" && !pending.submitting) {
    event.preventDefault();
    closeRegistrationConfirmation();
    return;
  }
  if (event.key !== "Tab" || !els.registrationConfirmationModal) return;
  const focusable = [...els.registrationConfirmationModal.querySelectorAll("button:not([disabled]), summary, [tabindex]:not([tabindex='-1'])")]
    .filter((element) => !element.hidden);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

async function prepareRegistrationConfirmation() {
  const draft = state.registrationDraft;
  const session = currentSession();
  if (!draft || !session) return;
  if (!draft.backendWorkspaceId || !draft.packagePlanId) {
    setRegistrationNotice("Publishing unavailable", "Save or reopen this Workspace, then check its files before publishing.", true);
    return;
  }
  const trigger = document.querySelector(".registration-apply");
  if (trigger) {
    trigger.disabled = true;
    trigger.textContent = "Checking current files…";
  }
  const originalWorkspaceId = draft.workspaceId;
  try {
    await syncPackagePlanEdits(draft);
    const validated = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans/${encodeURIComponent(draft.packagePlanId)}/validate`, {});
    if (validated.workspace) mergeUiWorkspace(validated.workspace);
    keepWorkspaceSelected(originalWorkspaceId);
    const checkedSession = currentSession();
    if (!checkedSession) throw new Error("The Workspace is no longer available.");
    state.registrationDraft = buildRegistrationDraftFromPackagePlan(checkedSession, validated.package_plan || {});
    const refreshed = state.registrationDraft;
    if (!packagePlanCheckPassed(refreshed.packagePlan)) {
      setRegistrationNotice("Check found problems", "Fix the listed configuration or source problems, then check again.", true);
      return;
    }
    if (!planCanApply(refreshed)) {
      setRegistrationNotice(
        "Test required",
        packagePlanCanSmoke(refreshed.packagePlan)
          ? "These checked files need a passing test before publishing."
          : "The required test is unavailable for this Catalog item.",
        true,
      );
      return;
    }
    state.registrationNotice = {
      title: "Checked version refreshed",
      body: "The confirmation below describes the exact current files that will be published.",
      error: false,
    };
    keepWorkspaceSelected(originalWorkspaceId);
    renderRegistrationExperience();
    openRegistrationConfirmation(state.registrationDraft);
  } catch (error) {
    keepWorkspaceSelected(originalWorkspaceId);
    setRegistrationNotice("Check before publishing failed", String(error.message || error), true);
  }
}

async function confirmCheckedRegistration() {
  const pending = state.pendingRegistrationConfirmation;
  const session = pending && state.sessions.find((item) => item.id === pending.workspaceId);
  if (!pending || !session || pending.submitting || !pending.sourceMatches) return;
  pending.submitting = true;
  pending.error = "";
  renderRegistrationConfirmation();
  try {
    const applied = await postJson(`/api/workspaces/${encodeURIComponent(pending.backendWorkspaceId)}/package-plans/${encodeURIComponent(pending.packagePlanId)}/apply`, {});
    if (applied.workspace) {
      const refreshed = mergeUiWorkspace(applied.workspace);
      if (refreshed) Object.assign(session, refreshed);
    }
    state.registrationDraft = buildRegistrationDraftFromPackagePlan(session, applied.package_plan || {});
    if (!applied.applied) {
      throw new Error("Check the files and complete any required test before publishing.");
    }
    const planBindings = captureStudyPlanCatalogBindings();
    await loadCatalogAndCompatibility();
    remapStudyPlansToRealmCatalogEntries(planBindings);
    closeRegistrationConfirmation({ restoreFocus: false });
    state.registrationNotice = { title: "Published", body: "The checked version is now an immutable Catalog version.", error: false };
    openRegisteredCatalogResult(applied);
  } catch (error) {
    const message = String(error.message || error);
    if (/changed after validation|changed after Check|check .* again/i.test(message)) {
      closeRegistrationConfirmation({ restoreFocus: false });
      setRegistrationNotice("Changes need checking", "The publication files changed after confirmation. Check them again before publishing.", true);
      return;
    }
    pending.submitting = false;
    pending.error = message;
    renderRegistrationConfirmation();
  }
}

function bindRegistrationMenu() {
  if (state.assistantMode !== "registration" && state.workbenchMode !== "setup") return;
  document.querySelectorAll("[data-registration-target]").forEach((input) => {
    input.addEventListener("change", () => {
      const draft = state.registrationDraft;
      if (!draft) return;
      const target = draft.configs.find((item) => item.key === input.dataset.registrationTarget);
      if (target) target.selected = input.checked;
      renderRegistrationExperience();
    });
  });
  document.querySelectorAll("[data-resource-registration-field]").forEach((input) => {
    const eventName = input.tagName === "SELECT" ? "change" : "input";
    input.addEventListener(eventName, () => {
      const draft = state.registrationDraft;
      if (!draft) return;
      draft[input.dataset.resourceRegistrationField] = input.value;
      if (input.dataset.resourceRegistrationField === "setupRole") renderRegistrationExperience();
    });
  });
  document.querySelectorAll("[data-package-plan-list]").forEach((input) => {
    input.addEventListener("input", () => {
      const target = findPackagePlanTarget(input.dataset.packagePlanTarget);
      if (!target) return;
      const values = splitLines(input.value);
      if (input.dataset.packagePlanList === "source_hints") {
        target.source_hints = values.map((path) => ({ path, reason: "Added in Studio package plan review." }));
      } else {
        target[input.dataset.packagePlanList] = values;
      }
    });
  });
  document.querySelectorAll("[data-package-plan-json]").forEach((input) => {
    input.addEventListener("input", () => {
      const target = findPackagePlanTarget(input.dataset.packagePlanTarget);
      if (!target) return;
      try {
        target[input.dataset.packagePlanJson] = JSON.parse(input.value || "[]");
        input.classList.remove("field-error");
      } catch (_error) {
        input.classList.add("field-error");
      }
    });
  });
  document.querySelectorAll("[data-package-plan-study-smoke]").forEach((input) => {
    input.addEventListener("change", () => {
      const plan = state.registrationDraft && state.registrationDraft.packagePlan;
      if (!plan) return;
      const study = (plan.studies || []).find((item) => String(item.target_id || item.id) === input.dataset.packagePlanStudySmoke);
      if (study) study.smoke = input.checked;
    });
  });
  const discover = document.querySelector(".registration-discover");
  if (discover) discover.addEventListener("click", async () => {
    const session = currentSession();
    if (!session || !session.backendWorkspaceId) return;
    try {
      const payload = await postJson(`/api/workspaces/${encodeURIComponent(session.backendWorkspaceId)}/package-plans`, { refresh: true });
      state.registrationDraft = buildRegistrationDraftFromPackagePlan(session, payload.package_plan || {});
      setRegistrationNotice("Catalog items found", "The Publish page now reflects the current Workspace files.");
    } catch (error) {
      setRegistrationNotice("Could not inspect Workspace files", String(error.message || error), true);
    }
  });
  const validate = document.querySelector(".registration-validate");
  if (validate) validate.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    if (!draft || state.registrationActionPending) return;
    const originalWorkspaceId = draft.workspaceId;
    keepWorkspaceSelected(originalWorkspaceId);
    if (!draft.backendWorkspaceId) {
      keepWorkspaceSelected(originalWorkspaceId);
      setRegistrationNotice("Check unavailable", "Save or reopen this Workspace before checking files.", true);
      return;
    }
    state.registrationActionPending = "check";
    setRegistrationNotice(
      "Checking files",
      "Studio is checking the exact current files. Nothing has been published yet.",
    );
    try {
      if (!draft.packagePlanId) {
        const selectedPaths = draft.configs.filter((item) => item.selected).map((item) => item.backendPath || item.label);
        const created = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans`, { config_paths: selectedPaths });
        Object.assign(draft, buildRegistrationDraftFromPackagePlan(currentSession(), created.package_plan || {}));
      }
      await syncPackagePlanEdits(draft);
      const validated = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans/${encodeURIComponent(draft.packagePlanId)}/validate`, {});
      if (validated.workspace) {
        const refreshed = mergeUiWorkspace(validated.workspace);
        const active = currentSession();
        if (refreshed && active) Object.assign(active, refreshed);
      }
      state.registrationDraft = buildRegistrationDraftFromPackagePlan(currentSession(), validated.package_plan || {});
      setRegistrationNotice(
        validated.package_plan && validated.package_plan.validation && validated.package_plan.validation.valid ? "Files checked" : "Check found problems",
        validated.package_plan && validated.package_plan.validation && validated.package_plan.validation.valid
          ? "The exact files shown here passed static configuration and source checks."
          : "Fix the listed configuration or source problems, then check again.",
        !(validated.package_plan && validated.package_plan.validation && validated.package_plan.validation.valid),
      );
    } catch (error) {
      setRegistrationNotice("Check failed", String(error.message || error), true);
    } finally {
      if (state.registrationActionPending === "check") {
        state.registrationActionPending = "";
      }
      keepWorkspaceSelected(originalWorkspaceId);
      renderRegistrationExperience();
    }
  });
  const smoke = document.querySelector(".registration-smoke");
  if (smoke) smoke.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    if (
      !draft
      || !draft.backendWorkspaceId
      || !draft.packagePlanId
      || state.registrationActionPending
    ) return;
    const originalWorkspaceId = draft.workspaceId;
    state.registrationActionPending = "test";
    setRegistrationNotice(
      "Testing checked files",
      "Studio is running the supported test. You can stay on this Publish page.",
    );
    try {
      await syncPackagePlanEdits(draft);
      const result = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans/${encodeURIComponent(draft.packagePlanId)}/smoke`, { max_trials: 1, timeout_seconds: 120 });
      state.registrationDraft = buildRegistrationDraftFromPackagePlan(currentSession(), result.package_plan || {});
      setRegistrationNotice(
        result.smoke && result.smoke.valid ? "Test passed" : "Test failed",
        result.smoke && result.smoke.valid ? "The supported test completed successfully." : ((result.smoke && result.smoke.errors || []).join(" ") || "The test did not pass."),
        !(result.smoke && result.smoke.valid),
      );
    } catch (error) {
      setRegistrationNotice("Test failed", String(error.message || error), true);
    } finally {
      if (state.registrationActionPending === "test") {
        state.registrationActionPending = "";
      }
      keepWorkspaceSelected(originalWorkspaceId);
      renderRegistrationExperience();
    }
  });
  const apply = document.querySelector(".registration-apply");
  if (apply) apply.addEventListener("click", prepareRegistrationConfirmation);
  const openCatalog = document.querySelector(".registration-open-catalog");
  if (openCatalog) openCatalog.addEventListener("click", async () => {
    const session = currentSession();
    if (!session) return;
    await loadCatalogAndCompatibility();
    openRegisteredCatalogResult({ applied: true, workspace: session });
  });
  const resourceApply = document.querySelector(".registration-resource-apply");
  if (resourceApply) resourceApply.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    const session = currentSession();
    if (!draft || !session || !draft.backendWorkspaceId) return;
    try {
      const configured = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/setup/configure`, {
        role: draft.setupRole || "reference",
        id: draft.resourceId || slug(session.title || session.id || "component"),
        description: draft.resourceDescription || "",
      });
      if (configured.workspace) mergeUiWorkspace(configured.workspace);
      const result = configured.configuration || {};
      if (result.needs_editing) {
        const paths = (result.created_paths || []).join(", ");
        state.registrationDraft.setupRole = result.role || draft.setupRole;
        const detected = result.detected_simulation;
        const handoff = detected
          ? ` Studio detected the generated simulator and prefilled ${detected.parameter_count || 0} Candidate input${detected.parameter_count === 1 ? "" : "s"}. In optpilot_adapter.py, review the emitted metric_values; in the template, replace metrics.keys: [score] with those exact metric names.`
          : " Define the real Candidate contract and evaluation logic.";
        const enabledName = result.role === "method" ? "method.yaml" : "environment.yaml";
        setRegistrationNotice("Starter files created", `Edit ${paths}.${handoff} Rename the .template.yaml.disabled file to ${enabledName}, then choose Find Catalog items again. Nothing can be published until that reviewed config is enabled.`);
      } else {
        const prepared = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans`, { refresh: true });
        state.registrationDraft = buildRegistrationDraftFromPackagePlan(currentSession(), prepared.package_plan || {});
        setRegistrationNotice("Configuration ready", "Check the selected files, then publish that exact checked version.");
      }
    } catch (error) {
      setRegistrationNotice("Configuration failed", String(error.message || error), true);
    }
  });
  const curate = document.querySelector(".registration-curate-assistant");
  if (curate) curate.addEventListener("click", requestWorkspaceCuration);
}

async function requestWorkspaceCuration() {
  const draft = state.registrationDraft;
  const workspace = currentSession();
  const agentSession = currentAgentSession();
  if (!draft || !workspace || !agentSession || assistantIsBusy() || assistantIsAwaitingApproval()) return;
  if (!attachedWorkspaceIds(agentSession.id).includes(workspace.id)) {
    const attached = await attachWorkspaceToCurrent(workspace.id);
    if (!attached) {
      setRegistrationNotice(
        "Assistant access could not be added",
        `This Workspace was not made available to ${assistantSessionLabel(agentSession)}.`,
        true,
      );
      renderRegistrationExperience();
      return;
    }
  }
  const message = [
    "Inspect this Workspace and help me curate it into an ordinary OptPilot package.",
    "Determine which public roles (environment, method, resource, or a combination) are supported by the code, but do not guess semantic choices such as the candidate contract, objectives, metrics, or what a simulator parameter means.",
    "Ask me only for the smallest missing semantic decisions. Then add thin adapters and public config files under optpilot_configs/, preserve the generated output as normal source code, and use the existing Publish checks and Test workflow.",
    "Do not publish the Workspace until validation succeeds and I explicitly confirm the final checked version.",
  ].join(" ");
  const userMessage = ["user", "User", message];
  state.assistantMode = "chat";
  setAssistantOpen(true);
  pushAssistantMessage(userMessage);
  const priorStatus = agentSession.status;
  const priorEffectiveStatus = agentSession.effective_status;
  if (!agentSession.id.startsWith("agent-session-")) {
    agentSession.status = "running";
    agentSession.effective_status = "running";
  }
  const curate = document.querySelector(".registration-curate-assistant");
  if (curate) {
    curate.disabled = true;
    curate.textContent = "Opening Assistant...";
  }
  renderAssistant();
  try {
    const persisted = await persistAssistantMessage(userMessage, {
      keepalive: true,
      sessionId: agentSession.id,
      rethrowError: true,
    });
    if (!persisted) throw new Error("The Assistant conversation is not ready yet.");
    state.assistantMode = "chat";
  } catch (error) {
    agentSession.status = priorStatus;
    agentSession.effective_status = priorEffectiveStatus;
    pushAssistantMessage([
      "assistant",
      "Curation request not sent",
      boundedPublicActionError(
        error,
        "The backend Assistant could not accept this request. Refresh Studio and try again.",
      ),
    ], { persist: false });
  }
  renderAssistant();
}

function splitLines(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function findPackagePlanTarget(targetId) {
  const plan = state.registrationDraft && state.registrationDraft.packagePlan;
  if (!plan || !targetId) return null;
  return [...(plan.components || []), ...(plan.resources || [])].find((item) => String(item.target_id || item.id) === String(targetId)) || null;
}

async function syncPackagePlanEdits(draft) {
  const plan = draft && draft.packagePlan;
  if (!draft || !draft.backendWorkspaceId || !draft.packagePlanId || !plan) return null;
  const payload = {
    components: (plan.components || []).map(packagePlanTargetUpdate),
    resources: (plan.resources || []).map(packagePlanTargetUpdate),
    studies: (plan.studies || []).map((study) => ({
      target_id: study.target_id,
      smoke: Boolean(study.smoke),
    })),
  };
  const updated = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/package-plans/${encodeURIComponent(draft.packagePlanId)}/update`, payload);
  draft.packagePlan = updated.package_plan || plan;
  draft.packagePlanId = draft.packagePlan.id || draft.packagePlanId;
  return draft.packagePlan;
}

function packagePlanTargetUpdate(target) {
  return {
    target_id: target.target_id,
    include: target.include || [],
    exclude: target.exclude || [],
    source_hints: target.source_hints || [],
    path_rewrites: target.path_rewrites || [],
  };
}

function renderEmptyWorkspace() {
  updateSidebarCodeServerStatus();
  if (els.workspaceContextNotice) {
    els.workspaceContextNotice.hidden = true;
    els.workspaceContextNotice.classList.remove(
      "workspace-context-notice-error",
      "catalog-inspector-notice",
    );
    els.workspaceContextNotice.innerHTML = "";
  }
  els.sessionTitle.textContent = "No workspace selected";
  els.sessionPath.textContent = "Create a workspace or open one from Catalog, Candidates, or generated output.";
  els.sessionStatus.textContent = "idle";
  els.sessionStatus.className = "status-pill status-review";
  els.sessionSummary.innerHTML = "";
  els.sessionFiles.innerHTML = "";
  els.sessionContext.innerHTML = "";
  els.sessionTools.innerHTML = "";
  els.sessionWorkspaceActions.innerHTML = `<button class="file-tree-item open-session-code" type="button" disabled>No code folder selected</button>`;
  state.embeddedCodeUrl = "";
  state.embeddedCodeFolder = "";
  state.codeWorkspaceStatus = "idle";
  state.codeWorkspaceMessage = "Select or create a workspace to start editing.";
  if (els.embeddedCodeWorkspace) els.embeddedCodeWorkspace.removeAttribute("src");
  renderWorkspaceWorkbenchToolbar(null);
  renderPreviewWorkbench();
  renderWorkbenchMode();
  renderAssistant();
  renderSessionBottom();
}

async function selectSession(sessionId) {
  const selected = state.sessions.find((item) => item.id === sessionId);
  if (selected && selected.realmManaged && selected.reopenRequired) {
    const reopened = await reopenManagedWorkspace(selected);
    if (!reopened) return;
  }
  if (state.view !== "workspace") setView("workspace");
  const attachedToAssistant = attachedWorkspaceIds().includes(sessionId);
  setSelectedWorkspace(sessionId, { sync: attachedToAssistant });
  syncStudioRoute();
  const agentSession = attachedToAssistant ? currentAgentSession() : null;
  const selectedWorkspace = state.sessions.find((item) => item.id === sessionId);
  if (agentSession && selectedWorkspace && selectedWorkspace.backendWorkspaceId && !agentSession.id.startsWith("agent-session-")) {
    postJson(`/api/agent-sessions/${encodeURIComponent(agentSession.id)}/select-workspace`, { workspace_id: selectedWorkspace.backendWorkspaceId })
      .then((payload) => updateAgentSessionFromPayload(payload.session))
      .catch(() => {});
  }
  const next = currentSession();
  state.selectedFileKey = firstFileKey(next);
  if (isEmbeddedCodeWorkspaceActive() || shouldAutoOpenCodeWorkspace(next)) {
    await openCodeServerEmbedded();
    return;
  }
  renderWorkspace();
}

async function attachWorkspaceAndRender(workspaceId) {
  if (!workspaceId) return;
  const agentSession = await attachWorkspaceToCurrent(workspaceId);
  if (!agentSession) return;
  if (agentSession.id === state.selectedAgentSessionId) {
    state.workspaceNotice = {
      workspaceId,
      assistantSessionId: agentSession.id,
      title: `Available to ${assistantSessionLabel(agentSession)}`,
      body: "This Assistant conversation can now use the Workspace files. The Workspace still exists independently.",
      error: false,
    };
  }
  if (state.view !== "workspace") setView("workspace");
  state.selectedFileKey = firstFileKey(currentSession());
  await loadUiWorkspaces();
  rebuildDerivedState();
  state.assistantMode = "chat";
  setAssistantOpen(true);
  renderWorkspace();
  renderAssistant();
  window.requestAnimationFrame(() => {
    if (els.agentInput) els.agentInput.focus();
  });
}

function startAssistantResize(event) {
  if (!state.assistantOpen) return;
  event.preventDefault();
  if (document.body.classList.contains("resizing-assistant")) return;
  const panel = document.querySelector(".agent-panel");
  if (!panel) return;
  const isMouseEvent = event.type === "mousedown";
  const moveEventName = isMouseEvent ? "mousemove" : "pointermove";
  const upEventName = isMouseEvent ? "mouseup" : "pointerup";
  if (!isMouseEvent && event.currentTarget && event.currentTarget.setPointerCapture) {
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch (error) {
      // Some synthetic pointer events do not support capture.
    }
  }
  document.body.classList.add("resizing-assistant");
  const onMove = (moveEvent) => {
    const panelRect = panel.getBoundingClientRect();
    const width = Math.round(moveEvent.clientX - panelRect.left);
    state.assistantPanelWidth = Math.max(280, Math.min(560, width));
    document.documentElement.style.setProperty("--assistant-panel-width", `${state.assistantPanelWidth}px`);
  };
  const onUp = () => {
    if (!isMouseEvent && event.currentTarget && event.currentTarget.releasePointerCapture) {
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch (error) {
        // Capture may already be released if the browser cancelled the drag.
      }
    }
    document.body.classList.remove("resizing-assistant");
    window.removeEventListener(moveEventName, onMove);
    window.removeEventListener(upEventName, onUp);
    renderAssistant();
  };
  window.addEventListener(moveEventName, onMove);
  window.addEventListener(upEventName, onUp, { once: true });
}

function setSelectedAgentSessionState(sessionId) {
  if (
    state.workspaceNotice
    && state.workspaceNotice.assistantSessionId
    && state.workspaceNotice.assistantSessionId !== sessionId
  ) {
    state.workspaceNotice = null;
  }
  state.selectedAgentSessionId = sessionId;
  storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
}

async function selectAgentSession(sessionId) {
  setSelectedAgentSessionState(sessionId);
  state.assistantMode = "chat";
  renderWorkspace();
  renderAssistant();
}

async function createAgentSession() {
  try {
    const payload = await postJson("/api/agent-sessions", {
      title: `Conversation ${state.agentSessions.length + 1}`,
      description: "New conversation",
      attached_workspace_ids: [],
      selected_workspace_id: "",
    });
    await updateAgentSessionFromPayload(payload.session);
    setSelectedAgentSessionState(payload.session.id);
  } catch (error) {
    const id = `agent-session-${Date.now().toString(36)}`;
    const index = state.agentSessionSeq++;
    const session = {
      id,
      title: `Conversation ${index}`,
      description: "New conversation",
      createdAt: "now",
    };
    state.agentSessions = [session, ...state.agentSessions];
    state.agentWorkspaceAttachments[id] = [];
    state.selectedWorkspaceByAgentSession[id] = null;
    state.assistantMessagesBySession[id] = defaultAssistantMessages();
    state.agentEventsBySession[id] = [];
    setSelectedAgentSessionState(id);
  }
  state.assistantMode = "chat";
  renderWorkspace();
  setAssistantOpen(true);
}

async function closeWorkspaceFromCurrentSession(workspaceId) {
  const agentSession = currentAgentSession();
  if (!agentSession) return;
  await detachWorkspaceFromSession(workspaceId, agentSession.id, { announce: true });
}

async function detachWorkspaceFromSession(workspaceId, agentSessionId, options = {}) {
  const workspace = state.sessions.find((item) => item.id === workspaceId);
  const label = workspace ? workspace.title : "this workspace";
  const agentSession = state.agentSessions.find((item) => item.id === agentSessionId) || currentAgentSession();
  if (!agentSession) return;
  state.agentWorkspaceAttachments[agentSession.id] = attachedWorkspaceIds(agentSession.id).filter((id) => id !== workspaceId);
  if (!agentSession.id.startsWith("agent-session-")) {
    try {
      const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(agentSession.id)}/detach-workspace`, { workspace_id: workspaceId });
      if (payload.session) mergeAgentSessionPayload(payload.session);
    } catch (error) {
      // Keep the optimistic UI state; a refresh will reconcile if needed.
    }
  }
  if (workspace && workspace.backendWorkspaceId) {
    try {
      const payload = await postJson(`/api/workspaces/${encodeURIComponent(workspace.backendWorkspaceId)}/detach`, { session_id: agentSession.id });
      if (payload.workspace) {
        mergeUiWorkspace(payload.workspace);
      }
    } catch (error) {
      // Session detach already succeeded; workspace record can be refreshed later.
    }
  }
  if (options.announce && agentSession.id === state.selectedAgentSessionId) {
    state.workspaceNotice = {
      workspaceId,
      assistantSessionId: agentSession.id,
      title: `Removed from ${assistantSessionLabel(agentSession)}`,
      body: `${label} remains in Workspaces with all of its files.`,
      error: false,
    };
  }
  await loadUiWorkspaces();
  rebuildDerivedState();
  renderWorkspace();
}

function renderWorkspaceCleanupModal() {
  if (!els.workspaceCleanupModal) return;
  const pending = state.pendingWorkspaceCleanup;
  const workspace = pending && state.sessions.find((item) => item.id === pending.workspaceId);
  els.workspaceCleanupModal.hidden = !pending;
  if (!pending || !workspace) return;
  const destructiveLabel = workspaceDestructiveLabel(workspace);
  const isCatalogCopy = workspace.sourceType === "catalog-copy";
  const attachedNames = (workspace.attachedSessions || [])
    .map((sessionId) => state.agentSessions.find((item) => item.id === sessionId))
    .filter(Boolean)
    .map((session) => session.title);
  if (els.workspaceCleanupTitle) {
    els.workspaceCleanupTitle.textContent = `${destructiveLabel}: ${workspace.title}`;
  }
  if (els.workspaceCleanupBody) {
    const destructiveDescription = workspace.realmManaged
      ? "OptPilot will delete this Workspace and its managed files. Published Catalog versions and source Runs remain unchanged."
      : workspace.managedByStudio
      ? isCatalogCopy
        ? "OptPilot will delete this editable copy. The original Catalog version remains unchanged."
        : "OptPilot will delete this Workspace and its managed files."
      : "OptPilot will remove this Workspace from the list. The linked local folder and its files remain on disk.";
    els.workspaceCleanupBody.textContent = attachedNames.length
      ? `${destructiveDescription} First remove it from these Assistant conversations: ${attachedNames.join(", ")}.`
      : destructiveDescription;
  }
  if (els.workspaceCleanupDeleteButton) {
    els.workspaceCleanupDeleteButton.textContent = destructiveLabel;
    els.workspaceCleanupDeleteButton.disabled = attachedNames.length > 0;
    els.workspaceCleanupDeleteButton.title = attachedNames.length
      ? "Remove this Workspace from every named Assistant conversation first."
      : destructiveLabel;
  }
}

function cancelPendingWorkspaceDelete() {
  state.pendingWorkspaceCleanup = null;
  renderWorkspaceCleanupModal();
}

async function deletePendingWorkspaceDraft() {
  const pending = state.pendingWorkspaceCleanup;
  state.pendingWorkspaceCleanup = null;
  renderWorkspaceCleanupModal();
  if (!pending) return;
  await deleteWorkspaceDraft(pending.workspaceId);
}

async function requestWorkspaceDelete(workspaceId) {
  const workspace = state.sessions.find((item) => item.id === workspaceId);
  if (!workspace) return;
  state.pendingWorkspaceCleanup = { workspaceId, sessionId: state.selectedAgentSessionId || "", intent: "delete" };
  renderWorkspaceCleanupModal();
}

async function deleteWorkspaceDraft(workspaceId) {
  const workspace = state.sessions.find((item) => item.id === workspaceId);
  const label = workspace ? workspace.title : "Draft workspace";
  try {
    const payload = await deleteJson(`/api/workspaces/${encodeURIComponent(workspaceId)}`);
    const deleted = payload.workspace || {};
    const isCatalogCopy = workspace && workspace.sourceType === "catalog-copy";
    state.uiWorkspaces = state.uiWorkspaces.filter((item) => item.id !== workspaceId);
    Object.keys(state.agentWorkspaceAttachments).forEach((sessionId) => {
      state.agentWorkspaceAttachments[sessionId] = (state.agentWorkspaceAttachments[sessionId] || []).filter((id) => id !== workspaceId);
      if (state.selectedWorkspaceByAgentSession[sessionId] === workspaceId) {
        state.selectedWorkspaceByAgentSession[sessionId] = null;
      }
    });
    if (state.selectedSessionId === workspaceId) state.selectedSessionId = null;
    rebuildDerivedState();
    const title = deleted.workspace_retired ? "Workspace retired" : deleted.files_deleted ? "Workspace deleted" : "Workspace removed";
    const detail = deleted.workspace_retired
      ? `${label} was deleted. Its published Catalog version was not changed.`
      : deleted.files_deleted
      ? isCatalogCopy
        ? `${label} was deleted from Studio workspace storage. The original catalog entry was not changed.`
        : `${label} was deleted from Studio-owned draft storage.`
      : `${label} was removed from Studio. The referenced folder was left on disk.`;
    pushAssistantMessage(["tool", title, detail]);
  } catch (error) {
    pushAssistantMessage(["tool", "Workspace removal failed", String(error.message || error)]);
    setAssistantOpen(true);
  }
  renderWorkspace();
  renderAssistant();
}

function workspaceDestructiveLabel(workspace) {
  if (!workspace) return "Remove from Workspaces";
  return workspace.ownership === "external-reference"
    ? "Remove from Workspaces"
    : "Delete Workspace";
}

function renderCodeServerCard(session) {
  updateSidebarCodeServerStatus();
}

function updateSidebarCodeServerStatus() {
  renderPlatformStatus();
}

function renderPlatformStatus() {
  const services = platformServices();
  const requiredBlocked = services.some((service) => service.required && service.level === "failed");
  const requiredWaiting = services.some((service) => service.required && service.level === "review");
  const summary = requiredBlocked
    ? ["Needs setup", "failed"]
    : requiredWaiting
    ? ["Starting", "review"]
    : ["Ready", "ready"];
  if (els.healthStatus) els.healthStatus.textContent = summary[0];
  if (els.sidebarServiceStatus) {
    els.sidebarServiceStatus.innerHTML = services.map(sidebarServiceRow).join("");
  }
}

function platformServices() {
  const code = state.codeServer || {};
  const runtime = state.runtime || {};
  const agent = state.agentRuntimeStatus || {};
  return [
    {
      label: "Studio",
      badge: state.platformReady ? "ready" : "offline",
      level: state.platformReady ? "ready" : "failed",
      detail: state.platformReady ? "Local UI serving" : "Local UI unreachable",
      required: true,
    },
    codeEditorService(code),
    openHandsService(agent),
    sandboxService(runtime),
  ];
}

function codeEditorService(status) {
  if (status.running) {
    return {
      label: "Code editing",
      badge: "running",
      level: "ready",
      detail: `Port ${status.port || 8766}${status.workspace_root ? ` - ${shortPath(status.workspace_root)}` : ""}`,
      required: false,
    };
  }
  if (status.installed || status.available) {
    return {
      label: "Code editing",
      badge: "ready",
      level: "review",
      detail: "Installed; start from Editor",
      required: false,
    };
  }
  return {
    label: "Code editing",
    badge: "missing",
    level: "review",
    detail: status.error || status.install_hint || "code-server not installed",
    required: false,
  };
}

function openHandsService(status) {
  if (status.enabled && status.connected) {
    return {
      label: "Assistant",
      badge: "connected",
      level: "ready",
      detail: status.model || "Agent server reachable",
      required: false,
    };
  }
  if (!status.enabled) {
    return {
      label: "Assistant",
      badge: "off",
      level: "review",
      detail: "Assistant runtime disabled",
      required: false,
    };
  }
  if (!status.credentials_configured) {
    return {
      label: "Assistant",
      badge: "setup",
      level: "review",
      detail: !status.model ? "Model missing" : "API key missing",
      required: false,
    };
  }
  if (status.server_configured) {
    return {
      label: "Assistant",
      badge: "offline",
      level: "review",
      detail: status.base_url || "Agent server not reachable",
      required: false,
    };
  }
  return {
    label: "Assistant",
    badge: "chat",
    level: "review",
    detail: "No agent server URL configured",
    required: false,
  };
}

function sandboxService(runtime) {
  const workspaceRuntime = runtime.workspace_runtime || {};
  if (workspaceRuntime.engine_available) {
    return {
      label: "Sandbox",
      badge: workspaceRuntime.engine || "ON",
      level: "ready",
      detail: workspaceRuntime.image || "Workspace containers ready",
      required: false,
    };
  }
  return {
    label: "Sandbox",
    badge: "OFF",
    level: "review",
    detail: workspaceRuntime.message || "Workspace container runtime unavailable",
    required: false,
  };
}

function sidebarServiceRow(service) {
  const title = `${service.label}: ${service.detail || service.badge || service.level}`;
  return `
    <div class="sidebar-service-row ${escapeHtml(service.level)}" title="${escapeHtml(title)}">
      <span class="service-dot ${escapeHtml(service.level)}" aria-hidden="true"></span>
      <span class="sidebar-service-label">${escapeHtml(service.label)}</span>
      <span class="sidebar-service-badge">${escapeHtml(compactServiceBadge(service))}</span>
    </div>
  `;
}

function compactServiceBadge(service) {
  return service.level === "ready" ? "ON" : "OFF";
}

function compactVersion(value) {
  return String(value || "").replace(/,\s*build\s+.*/i, "").trim();
}

function renderSessionEditor(session) {
  if (!session.files[state.selectedFileKey]) state.selectedFileKey = firstFileKey(session);
}

function renderWorkbenchMode() {
  const catalogSourceView = isCatalogSourceView();
  const hasWorkingInterface = Boolean(workspaceInterfaceConfig());
  const requestedMode = ["code", "preview", "setup"].includes(state.workbenchMode)
    ? state.workbenchMode
    : "code";
  const mode = (requestedMode === "preview" && !hasWorkingInterface)
    || (catalogSourceView && requestedMode === "setup")
    ? "code"
    : requestedMode;
  state.workbenchMode = mode;
  const session = currentSession();
  const grid = document.querySelector("#workspaceView .workspace-grid");
  if (grid) {
    grid.classList.toggle("workbench-focused", true);
    grid.classList.toggle("code-focused", mode === "code");
    grid.classList.toggle("preview-focused", mode === "preview");
  }
  document.querySelectorAll("[data-workbench-mode]").forEach((button) => {
    const buttonMode = button.dataset.workbenchMode;
    button.hidden = catalogSourceView
      ? buttonMode === "setup" || (buttonMode === "preview" && !hasWorkingInterface)
      : buttonMode === "preview" && !hasWorkingInterface;
    const selected = !button.hidden && buttonMode === mode;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
  });
  const tabList = document.querySelector(".workbench-mode-tabs");
  if (tabList) {
    tabList.setAttribute("aria-label", catalogSourceView ? "Catalog item views" : "Workspace views");
  }
  [
    ["code", els.codeWorkbench],
    ["preview", els.previewWorkbench],
    ["setup", els.setupWorkbench],
  ].forEach(([key, element]) => {
    if (!element) return;
    const active = key === mode;
    element.classList.toggle("active-workbench", active);
    element.setAttribute("aria-hidden", active ? "false" : "true");
  });
  if (session && els.embeddedCodeWorkspacePath) {
    els.embeddedCodeWorkspacePath.textContent = shortPath(session.codeFolder || session.path);
  } else if (els.embeddedCodeWorkspacePath) {
    els.embeddedCodeWorkspacePath.textContent = "-";
  }
  if (els.embeddedCodeWorkspaceEmpty) {
    renderCodeWorkspacePlaceholder();
  }
  if (els.pauseCodeWorkspaceButton) {
    els.pauseCodeWorkspaceButton.disabled = state.codeWorkspaceStatus === "opening" || !state.embeddedCodeUrl;
  }
  if (els.reloadEmbeddedCodeButton) {
    els.reloadEmbeddedCodeButton.disabled = state.codeWorkspaceStatus === "opening" || !state.embeddedCodeUrl;
  }
  renderWorkspaceWorkbenchToolbar(session);
  renderPreviewWorkbench();
  renderWorkspaceSetup();
  renderAssistant();
  renderActiveInterfaceIndicator();
}

function renderWorkspaceWorkbenchToolbar(session = currentSession()) {
  const catalogSourceView = isCatalogSourceView(session);
  const catalogComponent = catalogSourceView ? catalogSourceComponent(session) : null;
  if (els.workspaceTitleInput) {
    els.workspaceTitleInput.setAttribute("aria-label", catalogSourceView ? "Catalog item name" : "Workspace name");
    els.workspaceTitleInput.disabled = !session || Boolean(session && session.mode === "read-only");
    els.workspaceTitleInput.placeholder = catalogSourceView ? "Catalog item" : session ? "Workspace name" : "No Workspace selected";
    els.workspaceTitleInput.title = !session
      ? "Select a Workspace first."
      : session.mode === "read-only"
      ? "Catalog item names cannot be changed here."
      : "Rename this Workspace. Press Enter or click elsewhere to save.";
    if (document.activeElement !== els.workspaceTitleInput) {
      els.workspaceTitleInput.value = catalogComponent
        ? String(catalogComponent.entry && catalogComponent.entry.label || session.title)
        : session ? session.title : "";
    }
  }
  if (els.workspaceCommitButton) {
    els.workspaceCommitButton.hidden = !session || !session.realmManaged;
    els.workspaceCommitButton.disabled = !session || session.reopenRequired || !session.workspaceRevision;
    els.workspaceCommitButton.textContent = session && session.lastCommitStatus === "unchanged"
      ? "No Changes"
      : "Commit Workspace";
  }
  if (els.openWorkspaceExternalButton) {
    const mode = ["preview", "setup"].includes(state.workbenchMode)
      ? state.workbenchMode
      : "code";
    const preview = currentWorkspacePreview(session);
    const catalogLaunch = catalogSourceView ? currentCatalogInterfaceLaunch(session) : null;
    const previewUrl = catalogSourceView ? catalogInterfacePreviewUrl(session) : preview.url;
    const openingPreview = catalogSourceView
      ? Boolean(catalogLaunch && ["queued", "running", "stopping"].includes(catalogLaunch.status))
      : preview.status === "opening";
    const codeOpening = state.codeWorkspaceStatus === "opening";
    els.openWorkspaceExternalButton.hidden = mode === "setup" || (catalogSourceView && mode !== "preview");
    els.openWorkspaceExternalButton.textContent = mode === "preview"
      ? "Open interface in new window"
      : "Open editor in new window";
    els.openWorkspaceExternalButton.title = mode === "preview"
      ? "Open the running interface outside Studio."
      : "Open this Workspace in a separate code editor window.";
    els.openWorkspaceExternalButton.disabled = !session
      || session.reopenRequired
      || (mode === "preview" ? (!previewUrl || openingPreview) : codeOpening);
  }
}

function handleWorkspaceTitleKeydown(event) {
  if (event.key === "Enter") {
    event.preventDefault();
    event.currentTarget.blur();
    return;
  }
  if (event.key === "Escape") {
    const session = currentSession();
    event.currentTarget.value = session ? session.title : "";
    event.currentTarget.blur();
  }
}

async function saveWorkspaceTitleFromInput() {
  const session = currentSession();
  const input = els.workspaceTitleInput;
  if (!session || !input || input.disabled) return;
  const title = input.value.trim().replace(/\s+/g, " ");
  if (!title) {
    input.value = session.title;
    return;
  }
  if (title === session.title) return;
  input.disabled = true;
  state.workspaceNotice = null;
  try {
    const payload = await postJson(
      `/api/workspaces/${encodeURIComponent(session.backendWorkspaceId || session.id)}/rename`,
      {
        schema: "optpilot.studio-workspace-rename-request.v1",
        request_id: newRequestId(),
        title,
        expected_title: session.title,
        expected_metadata_revision: session.realmManaged
          ? session.workspaceMetadataRevision
          : null,
      },
    );
    if (payload.workspace) {
      mergeUiWorkspace(payload.workspace);
      if (state.registrationDraft && state.registrationDraft.workspaceId === session.id) {
        state.registrationDraft.workspaceTitle = payload.workspace.title || title;
      }
    }
  } catch (error) {
    await loadUiWorkspaces();
    rebuildDerivedState();
    const current = state.sessions.find((item) => item.id === session.id);
    input.value = current ? current.title : session.title;
    state.workspaceNotice = {
      workspaceId: session.id,
      title: "Name not changed",
      body: boundedPublicActionError(
        error,
        "Refresh the Workspace and try again.",
      ),
      error: true,
    };
  } finally {
    input.disabled = false;
    renderWorkspace();
    renderAssistant();
  }
}

async function commitManagedWorkspace() {
  const session = currentSession();
  if (!session || !session.realmManaged || session.reopenRequired || !session.workspaceRevision) return;
  const button = els.workspaceCommitButton;
  if (button) {
    button.disabled = true;
    button.textContent = "Committing...";
  }
  try {
    const payload = await postJson(
      `/api/workspaces/${encodeURIComponent(session.backendWorkspaceId || session.id)}/commit`,
      { expected_workspace_revision: session.workspaceRevision },
    );
    const updated = mergeUiWorkspace(payload.workspace);
    const commit = payload.commit || {};
    const unchanged = commit.status === "unchanged";
    pushAssistantMessage([
      "tool",
      unchanged ? "Workspace unchanged" : "Workspace committed",
      unchanged
        ? `${session.title} remains at revision ${commit.current_revision || session.workspaceRevision}.`
        : `${session.title} advanced to revision ${commit.current_revision || updated && updated.workspaceRevision || ""}.`,
    ]);
  } catch (error) {
    pushAssistantMessage(["tool", "Workspace commit failed", String(error.message || error)]);
    setAssistantOpen(true);
  } finally {
    renderWorkspace();
    renderAssistant();
  }
}

function renderCodeWorkspacePlaceholder() {
  const active = Boolean(state.embeddedCodeUrl);
  if (els.embeddedCodeWorkspace) {
    if (active) {
      if (els.embeddedCodeWorkspace.getAttribute("src") !== state.embeddedCodeUrl) {
        els.embeddedCodeWorkspace.src = state.embeddedCodeUrl;
      }
      els.embeddedCodeWorkspace.style.display = "block";
    } else {
      els.embeddedCodeWorkspace.removeAttribute("src");
      els.embeddedCodeWorkspace.style.display = "none";
    }
  }
  els.embeddedCodeWorkspaceEmpty.style.display = active ? "none" : "grid";
  if (active) return;
  const status = state.codeWorkspaceStatus || "idle";
  const details = {
    detached: [
      "No Workspace selected",
      state.codeWorkspaceMessage || "Select, create, or open a Workspace to start editing.",
      "Create Workspace",
      false,
    ],
    error: [
      "Code Server unavailable",
      state.codeWorkspaceMessage || "Code Server could not open this workspace. Check the server logs or retry.",
      "Retry Code Server",
      false,
    ],
    opening: [
      "Opening Code Server",
      state.codeWorkspaceMessage || "Preparing the selected workspace folder in Code Server.",
      "Opening...",
      true,
    ],
    paused: [
      "Code Server paused",
      "Start the selected workspace folder when you are ready to inspect or edit code.",
      "Start Code Server",
      false,
    ],
    idle: [
      "Starting Code Server",
      "OptPilot is preparing the selected workspace folder.",
      "Start Code Server",
      false,
    ],
  }[status] || [
    "Start Code Server",
    "Inspect or edit this workspace without leaving OptPilot.",
    "Start Code Server",
    false,
  ];
  if (els.codeWorkspaceEmptyTitle) els.codeWorkspaceEmptyTitle.textContent = details[0];
  if (els.codeWorkspaceEmptyBody) els.codeWorkspaceEmptyBody.textContent = details[1];
  if (els.startEmbeddedCodeButton) {
    els.startEmbeddedCodeButton.textContent = details[2];
    els.startEmbeddedCodeButton.disabled = details[3];
  }
}

function renderPreviewWorkbench() {
  if (!els.previewWorkbench) return;
  const session = currentSession();
  if (isCatalogSourceView(session)) {
    renderCatalogInterfaceWorkbench(session);
    return;
  }
  const preview = currentWorkspacePreview(session);
  const hasWorkspace = Boolean(session);
  const hasPreview = Boolean(hasWorkspace && preview.url);
  const opening = preview.status === "opening";
  const workspaceInterfaceProfiles = summarizedInterfaceProfiles(session && session.interface);
  const workspaceInterface = workspaceInterfaceConfig(session);
  const workspaceInterfaceCapability = workspaceInterface
    ? workspaceInterfaceLaunchCapability(session, workspaceInterface)
    : null;
  const workspaceInterfaceUnavailable = Boolean(
    workspaceInterfaceCapability && workspaceInterfaceCapability.eligible !== true,
  );
  const workspaceInterfaceReason = workspaceInterfaceUnavailable
    ? String(workspaceInterfaceCapability.reason || "This interface is unavailable.")
    : "";
  const workspaceLaunch = currentWorkspaceInterfaceLaunch(session);
  els.previewWorkbench.classList.toggle("interface-launch-active", Boolean(workspaceLaunch));
  const otherInterfaceLaunch = isActiveInterfaceLaunch(state.interfaceLaunch)
    && !workspaceLaunch
    ? state.interfaceLaunch
    : null;
  renderInterfaceConflictActions(otherInterfaceLaunch);
  const otherInterfaceReason = otherInterfaceLaunch
    ? state.interfaceReturnError
      || `${otherInterfaceLaunch.label || "Another interface"} is already running in this tab. Return to it or stop it before launching this Workspace’s interface.`
    : "";
  const launchingInterface = Boolean(
    workspaceLaunch && ["queued", "running", "stopping"].includes(workspaceLaunch.status),
  );
  const failedInterfaceLaunch = Boolean(workspaceLaunch && workspaceLaunch.status === "failed");
  if (els.workspacePreviewPort && document.activeElement !== els.workspacePreviewPort) {
    els.workspacePreviewPort.value = String(preview.port || 5173);
  }
  if (els.workspacePreviewStatus) {
    const status = hasPreview
      ? `Port ${preview.port} in ${session.title}`
      : !hasWorkspace
      ? "Select a Workspace before opening an interface."
      : otherInterfaceReason
      ? otherInterfaceReason
      : workspaceInterfaceUnavailable
        ? workspaceInterfaceReason
        : workspaceInterface
        ? `Launch ${workspaceInterface.label || "the declared interface"} from ${session.title}.`
        : "This Workspace does not declare an interface.";
    els.workspacePreviewStatus.textContent = preview.message || status;
  }
  if (els.workspacePreviewFrame) {
    if (hasPreview) {
      if (els.workspacePreviewFrame.getAttribute("src") !== preview.url) {
        els.workspacePreviewFrame.src = preview.url;
      }
      els.workspacePreviewFrame.style.display = "block";
    } else {
      els.workspacePreviewFrame.removeAttribute("src");
      els.workspacePreviewFrame.style.display = "none";
    }
  }
  if (els.workspacePreviewEmpty) {
    els.workspacePreviewEmpty.style.display = hasPreview ? "none" : "grid";
  }
  if (els.workspacePreviewTitle) {
    els.workspacePreviewTitle.textContent = !hasWorkspace
      ? "No Workspace selected"
      : opening
      ? "Opening working interface"
      : otherInterfaceReason
      ? `${otherInterfaceLaunch.label || "Another interface"} is already running`
      : preview.status === "error"
      ? "Working interface unavailable"
      : "Launch this Workspace’s interface";
  }
  if (els.workspacePreviewBody) {
    els.workspacePreviewBody.textContent = !hasWorkspace
      ? "Select a Workspace that declares an interface."
      : opening
      ? `Preparing ${workspaceInterface && workspaceInterface.label || "the declared interface"}.`
      : otherInterfaceReason
      ? `${otherInterfaceReason} Interface launches are tracked independently in each browser tab.`
      : preview.status === "error"
      ? preview.message || "The preview could not be opened."
      : workspaceInterfaceUnavailable
      ? workspaceInterfaceReason
      : workspaceInterface
      ? "Run the interface declared by the current editable files."
      : "No working interface is declared for this Workspace.";
  }
  if (els.launchWorkspaceInterfaceButton) {
    renderWorkspaceInterfaceProfileSelector(session, workspaceInterfaceProfiles, workspaceInterface);
    if (workspaceInterface) {
      els.launchWorkspaceInterfaceButton.hidden = Boolean(otherInterfaceLaunch);
      els.launchWorkspaceInterfaceButton.disabled = workspaceInterfaceUnavailable || !hasWorkspace || Boolean(
        isActiveInterfaceLaunch(state.interfaceLaunch),
      );
      els.launchWorkspaceInterfaceButton.textContent = workspaceLaunch && workspaceLaunch.status === "ready"
        ? "Interface open"
        : workspaceLaunch && workspaceLaunch.status === "cleanup_pending"
        ? "Cleanup Pending"
        : failedInterfaceLaunch
        ? "Try interface again"
        : launchingInterface
        ? "Opening…"
        : hasPreview
        ? "Reopen interface"
        : otherInterfaceReason
        ? "Stop the running interface first"
        : "Launch interface";
      els.launchWorkspaceInterfaceButton.title = otherInterfaceReason || workspaceInterfaceReason
        || `Start ${workspaceInterface.label || workspaceInterface.id} from its declared profile.`;
    } else {
      els.launchWorkspaceInterfaceButton.hidden = true;
      els.launchWorkspaceInterfaceButton.disabled = true;
    }
  }
  if (els.openWorkspacePreviewButton) {
    els.openWorkspacePreviewButton.disabled = !hasWorkspace || opening || launchingInterface;
    els.openWorkspacePreviewButton.textContent = opening ? "Opening..." : "Open Preview";
  }
  if (els.reloadWorkspacePreviewButton) {
    els.reloadWorkspacePreviewButton.disabled = !hasPreview || opening;
  }
  renderWorkspaceInterfaceLaunchPanel(session, workspaceLaunch);
  renderWorkspaceWorkbenchToolbar(session);
}

function renderCatalogInterfaceWorkbench(session) {
  const component = catalogSourceComponent(session);
  const profiles = component ? componentInterfaceProfiles(component) : [];
  const selectedProfile = component ? componentSelectedInterfaceProfile(component) : null;
  const capability = component && selectedProfile
    ? componentInterfaceLaunchCapability(component, selectedProfile)
    : null;
  const unavailable = Boolean(capability && capability.eligible !== true);
  const unavailableReason = unavailable
    ? String(capability.reason || "This interface is unavailable.")
    : "";
  const launch = currentCatalogInterfaceLaunch(session);
  els.previewWorkbench.classList.toggle("interface-launch-active", Boolean(launch));
  const otherLaunch = isActiveInterfaceLaunch(state.interfaceLaunch)
    && !launch
    ? state.interfaceLaunch
    : null;
  renderInterfaceConflictActions(otherLaunch);
  const otherLaunchReason = otherLaunch
    ? state.interfaceReturnError
      || `${otherLaunch.label || "Another interface"} is already running in this browser tab. Return to it or stop it before starting this Catalog interface.`
    : "";
  const status = String(launch && launch.status || "");
  const opening = ["queued", "running"].includes(status);
  const stopping = status === "stopping";
  const cleanupPending = status === "cleanup_pending";
  const failed = status === "failed";
  const previewUrl = catalogInterfacePreviewUrl(session);
  const hasPreview = Boolean(previewUrl);
  const label = selectedProfile && (selectedProfile.label || selectedProfile.id)
    || launch && launch.label
    || "the declared interface";

  if (els.workspacePreviewPort && document.activeElement !== els.workspacePreviewPort) {
    els.workspacePreviewPort.value = String(selectedProfile && selectedProfile.presentation.port || 5173);
  }
  if (els.workspacePreviewStatus) {
    els.workspacePreviewStatus.textContent = hasPreview
      ? `${label} is running from the exact published Catalog version.`
      : otherLaunchReason || unavailableReason || `Run ${label} from the exact published Catalog version.`;
  }
  if (els.workspacePreviewFrame) {
    if (hasPreview) {
      if (els.workspacePreviewFrame.getAttribute("src") !== previewUrl) {
        els.workspacePreviewFrame.src = previewUrl;
      }
      els.workspacePreviewFrame.title = `${label} interface`;
      els.workspacePreviewFrame.style.display = "block";
    } else {
      els.workspacePreviewFrame.removeAttribute("src");
      els.workspacePreviewFrame.style.display = "none";
    }
  }
  if (els.workspacePreviewEmpty) {
    els.workspacePreviewEmpty.style.display = hasPreview ? "none" : "grid";
  }
  if (els.workspacePreviewTitle) {
    els.workspacePreviewTitle.textContent = otherLaunchReason
      ? `${otherLaunch.label || "Another interface"} is already running`
      : opening
      ? "Opening the published interface"
      : stopping
      ? "Stopping the interface"
      : cleanupPending
      ? "Interface cleanup needs attention"
      : failed
      ? "Interface could not start"
      : unavailable
      ? "Interface unavailable"
      : "Run the published interface";
  }
  if (els.workspacePreviewBody) {
    els.workspacePreviewBody.textContent = otherLaunchReason
      || (opening ? `Preparing ${label} in a temporary isolated runtime.` : "")
      || (stopping ? "The temporary interface runtime is being stopped." : "")
      || (cleanupPending ? "Execution stopped, but launch-scoped cleanup still needs to be retried." : "")
      || (failed ? launch.error || "The published interface did not become reachable." : "")
      || unavailableReason
      || "This starts a temporary isolated runtime from the exact Catalog version. No Workspace is created.";
  }
  renderCatalogInterfaceProfileSelector(component, profiles, selectedProfile);
  if (els.launchWorkspaceInterfaceButton) {
    const occupied = isActiveInterfaceLaunch(state.interfaceLaunch);
    els.launchWorkspaceInterfaceButton.hidden = !component || !selectedProfile || Boolean(otherLaunch);
    els.launchWorkspaceInterfaceButton.disabled = !component
      || !selectedProfile
      || unavailable
      || occupied;
    els.launchWorkspaceInterfaceButton.textContent = status === "ready"
      ? "Interface running"
      : cleanupPending
      ? "Cleanup pending"
      : stopping
      ? "Stopping…"
      : opening
      ? "Opening…"
      : failed
      ? "Try interface again"
      : otherLaunchReason
      ? "Stop the running interface first"
      : "Start interface";
    els.launchWorkspaceInterfaceButton.title = otherLaunchReason
      || unavailableReason
      || "Start this interface from the exact published Catalog version.";
  }
  if (els.openWorkspacePreviewButton) els.openWorkspacePreviewButton.disabled = true;
  if (els.reloadWorkspacePreviewButton) els.reloadWorkspacePreviewButton.disabled = !hasPreview;
  renderCatalogInterfaceLaunchPanel(component, launch);
  renderWorkspaceWorkbenchToolbar(session);
}

function renderCatalogInterfaceLaunchPanel(component, launchState) {
  const surface = els.workspaceInterfaceLaunchStatus;
  if (!surface) return;
  if (!component || !launchState) {
    surface.hidden = true;
    surface.innerHTML = "";
    return;
  }
  const disclosureState = captureInterfaceLaunchDisclosureState(surface);
  surface.hidden = false;
  surface.innerHTML = interfaceLaunchStatus(component, launchState);
  restoreInterfaceLaunchDisclosureState(surface, disclosureState, launchState);
  bindComponentInterfaceLaunchControls(component, surface);
}

function renderCatalogInterfaceProfileSelector(component, profiles, selected) {
  const existing = document.querySelector(".workspace-interface-profile-control");
  if (existing) existing.remove();
  if (!component || !els.launchWorkspaceInterfaceButton || profiles.length <= 1 || !selected) return;
  const control = document.createElement("label");
  control.className = "preview-port-control workspace-interface-profile-control";
  control.innerHTML = `
    <span>Profile</span>
    <select>${profiles.map((profile) => `<option value="${escapeHtml(profile.id)}" ${profile.id === selected.id ? "selected" : ""}>${escapeHtml(profile.label || profile.id)}</option>`).join("")}</select>
  `;
  const selector = control.querySelector("select");
  selector.disabled = Boolean(state.interfaceLaunch && state.interfaceLaunch.status !== "failed");
  selector.addEventListener("change", () => {
    state.interfaceProfileSelections[componentLaunchKey(component)] = selector.value;
    renderWorkspace();
  });
  els.launchWorkspaceInterfaceButton.parentElement.insertBefore(control, els.launchWorkspaceInterfaceButton);
}

function renderWorkspaceInterfaceLaunchPanel(session, launchState) {
  const surface = els.workspaceInterfaceLaunchStatus;
  if (!surface) return;
  if (!session || !launchState) {
    surface.hidden = true;
    surface.innerHTML = "";
    return;
  }
  const disclosureState = captureInterfaceLaunchDisclosureState(surface);
  surface.hidden = false;
  surface.innerHTML = workspaceInterfaceLaunchStatus(session, launchState);
  restoreInterfaceLaunchDisclosureState(surface, disclosureState, launchState);
  bindWorkspaceInterfaceLaunchControls(launchState);
}

function renderWorkspaceInterfaceProfileSelector(session, profiles, selected) {
  const existing = document.querySelector(".workspace-interface-profile-control");
  if (existing) existing.remove();
  if (!els.launchWorkspaceInterfaceButton || profiles.length <= 1 || !selected) return;
  const control = document.createElement("label");
  control.className = "preview-port-control workspace-interface-profile-control";
  control.innerHTML = `
    <span>Profile</span>
    <select>${profiles.map((profile) => `<option value="${escapeHtml(profile.id)}" ${profile.id === selected.id ? "selected" : ""}>${escapeHtml(profile.label || profile.id)}</option>`).join("")}</select>
  `;
  const selector = control.querySelector("select");
  selector.disabled = Boolean(state.interfaceLaunch && state.interfaceLaunch.status !== "failed");
  selector.addEventListener("change", () => {
    const key = workspaceInterfaceSelectionKey(session);
    state.interfaceProfileSelections[key] = selector.value;
    const nextProfile = profiles.find((profile) => profile.id === selector.value);
    const preview = currentWorkspacePreview(session);
    if (nextProfile) preview.port = Number(nextProfile.presentation.port);
    renderWorkspace();
  });
  els.launchWorkspaceInterfaceButton.parentElement.insertBefore(control, els.launchWorkspaceInterfaceButton);
}

function renderSessionBottom() {
  if (state.sessionTab === "preview") state.sessionTab = "terminal";
  document.querySelectorAll("[data-session-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sessionTab === state.sessionTab);
  });
  const session = currentSession();
  if (!session) {
    els.sessionBottom.innerHTML = emptyState("No Workspace selected.");
    return;
  }
  const content = {
    terminal: `<pre class="code-box terminal-box">${escapeHtml((session.terminal || []).join("\n"))}</pre>`,
    checks: `<div class="check-list">${(session.checks || []).map(checkRow).join("")}</div>`,
    diff: `<pre class="code-box terminal-box">--- Catalog version\n+++ ${escapeHtml(session.path)}\n@@\n+ changes stay in this Workspace until you publish a new Catalog version\n</pre>`,
  };
  els.sessionBottom.innerHTML = content[state.sessionTab] || content.terminal;
}

function renderCatalog() {
  if (els.componentSearch && els.componentSearch.value !== state.componentSearch) {
    els.componentSearch.value = state.componentSearch;
  }
  renderCatalogPackageFilter();
  document.querySelectorAll("[data-component-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.componentFilter === state.componentFilter);
  });
  renderConfiguredCatalogSources();
  const allCatalogComponents = allComponents();
  const query = normalizeSearch(state.componentSearch);
  const components = allCatalogComponents.filter((item) => {
    const matchesFilter = state.componentFilter === "all" || item.kind === state.componentFilter;
    const matchesPackage = state.componentPackageFilter === "all" || componentPackageId(item) === state.componentPackageFilter;
    const matchesSearch = !query || catalogSearchText(item).includes(query);
    return matchesFilter && matchesPackage && matchesSearch;
  });
  if (!components.some((item) => item.key === state.selectedComponentKey)) {
    state.selectedComponentKey = components[0] && components[0].key;
  }
  const initialLoading = state.catalogLoading && !state.catalogLoaded;
  const loadNotice = catalogLoadNotice();
  const componentHtml = components.map(componentButton).join("");
  const emptyMessage = allCatalogComponents.length
    ? "No Catalog items match these filters."
    : "No Catalog items have been published.";
  els.componentList.innerHTML = initialLoading
    ? loadNotice
    : `${loadNotice}${componentHtml || (state.catalogError ? "" : emptyInline(emptyMessage))}`;
  const retry = els.componentList.querySelector(".catalog-load-retry");
  if (retry) {
    retry.addEventListener("click", () => {
      void loadCatalogAndCompatibility({ strict: false });
    });
  }
  document.querySelectorAll("[data-component-key]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedComponentKey = button.dataset.componentKey;
      syncStudioRoute();
      renderCatalog();
    });
  });
  if (initialLoading) {
    els.componentDetail.innerHTML = emptyState("Catalog items will appear here when loading finishes.");
  } else if (state.catalogError && !components.length) {
    els.componentDetail.innerHTML = emptyState("Catalog items are unavailable. Try loading them again.");
  } else {
    renderComponentDetail();
  }
}

function catalogLoadNotice() {
  if (state.catalogLoading && !state.catalogLoaded) {
    return `
      <div class="catalog-load-notice" role="status" aria-live="polite">
        <strong>Loading Catalog…</strong>
        <span>Reusable items will appear when they are ready.</span>
      </div>
    `;
  }
  if (state.catalogLoading) {
    return `
      <div class="catalog-load-notice" role="status" aria-live="polite">
        <strong>Refreshing Catalog…</strong>
        <span>The current items remain available.</span>
      </div>
    `;
  }
  if (!state.catalogError) return "";
  return `
    <div class="catalog-load-notice error" role="alert">
      <span><strong>Catalog could not be loaded.</strong> ${escapeHtml(state.catalogError)}</span>
      <button class="ghost-button catalog-load-retry" type="button">Retry</button>
    </div>
  `;
}

function renderConfiguredCatalogSources() {
  if (!els.catalogSources) return;
  const sources = Array.isArray(state.catalog.sources) ? state.catalog.sources : [];
  if (!sources.length) {
    els.catalogSources.innerHTML = "";
    els.catalogSources.style.display = "none";
    return;
  }
  const existingDisclosure = els.catalogSources.querySelector(".catalog-source-disclosure");
  const preserveOpen = Boolean(existingDisclosure && existingDisclosure.open);
  const publishedCount = sources.filter((source) => (
    source.realm_head && typeof source.realm_head === "object"
  )).length;
  const revealFeedback = sources.some((source) => {
    const local = state.configuredSourceWorkspaceActions[String(source.source_id || "")] || {};
    return Boolean(local.pending || local.error);
  });
  const sourceSummary = `${sources.length} · ${publishedCount} published`;
  const sourceRows = sources.map((source) => {
    const sourceId = String(source.source_id || "");
    const setup = source.actions && source.actions.open_workspace || {};
    const local = state.configuredSourceWorkspaceActions[sourceId] || {};
    const head = source.realm_head && typeof source.realm_head === "object" ? source.realm_head : null;
    const disabled = local.pending || setup.eligible !== true;
    const sourceLabel = String(source.label || source.package_id || "Configured package");
    const headText = head ? `Published · Catalog revision ${head.revision}` : "Not published";
    const actionText = local.pending
      ? "Opening…"
      : local.error
      ? "Try opening again"
      : "Open Workspace";
    const reason = String(setup.reason || "Publishing from this Workspace is unavailable.");
    return `
      <section class="configured-source-card" data-configured-source-id="${escapeHtml(sourceId)}">
        <div class="configured-source-heading">
          <strong title="${escapeHtml(sourceLabel)}">${escapeHtml(sourceLabel)}</strong>
          <span>${escapeHtml(headText)}</span>
        </div>
        <button class="ghost-button configured-source-open" data-open-configured-source="${escapeHtml(sourceId)}" type="button" aria-label="Open ${escapeHtml(sourceLabel)} as a Workspace" ${disabled ? `disabled title="${escapeHtml(reason)}"` : `title="Open this local package as an editable Workspace"`}>${escapeHtml(actionText)}</button>
        ${setup.eligible === true ? "" : `<p class="source-note">${escapeHtml(reason)}</p>`}
        ${local.error ? `<p class="error-text configured-source-error" role="alert">${escapeHtml(local.error)}</p>` : ""}
      </section>
    `;
  }).join("");
  els.catalogSources.style.display = "block";
  els.catalogSources.innerHTML = `
    <details class="catalog-source-disclosure" ${preserveOpen || revealFeedback ? "open" : ""}>
      <summary class="catalog-source-summary" title="Local package folders connected to this Catalog">
        <strong>Local packages</strong>
        <span>${escapeHtml(sourceSummary)}</span>
      </summary>
      <div class="catalog-source-rows">
        ${sourceRows}
      </div>
    </details>
  `;
  els.catalogSources.querySelectorAll("[data-open-configured-source]").forEach((button) => {
    button.addEventListener("click", () => openConfiguredCatalogSourceWorkspace(button.dataset.openConfiguredSource));
  });
}

async function openConfiguredCatalogSourceWorkspace(sourceId) {
  const source = (state.catalog.sources || []).find((item) => item.source_id === sourceId);
  if (!source) return;
  const setup = source.actions && source.actions.open_workspace || {};
  if (setup.eligible !== true) return;
  const previous = state.configuredSourceWorkspaceActions[sourceId] || {};
  if (previous.pending) return;
  state.configuredSourceWorkspaceActions[sourceId] = {
    pending: true,
    error: "",
  };
  renderConfiguredCatalogSources();
  try {
    const payload = await postJson(
      `/api/catalog/sources/${encodeURIComponent(sourceId)}/workspace`,
      { schema: "optpilot.configured-source-workspace.v1" },
    );
    const workspace = mergeUiWorkspace(payload.workspace);
    if (!workspace) throw new Error("Studio did not return the configured source Workspace.");
    delete state.configuredSourceWorkspaceActions[sourceId];
    setSelectedWorkspace(workspace.id);
    state.workbenchMode = "setup";
    setView("workspace");
    await openRegistrationMenu();
  } catch (error) {
    state.configuredSourceWorkspaceActions[sourceId] = {
      pending: false,
      error: String(error.message || "The configured source Workspace could not be opened."),
    };
    renderConfiguredCatalogSources();
  }
}

function captureStudyPlanCatalogBindings() {
  return state.plans.map((plan) => ({
    plan,
    bindings: Object.fromEntries(["study", "environment", "method"].map((kind) => {
      const current = plan[kind];
      const entries = kind === "study"
        ? state.catalog.studies || []
        : kind === "environment"
        ? state.catalog.environments || []
        : state.catalog.methods || [];
      const listed = current && entries.find((entry) => entry.uid === current.uid);
      const originalRef = exactCatalogEntryRef(current) || exactCatalogEntryRef(listed);
      return [kind, {
        package_id: current && current.package_id || listed && listed.package_id || "",
        id: current && current.id || listed && listed.id || "",
        source_kind: originalRef && typeof originalRef === "object" ? originalRef.source_kind : "",
      }];
    })),
  }));
}

function remapStudyPlansToRealmCatalogEntries(planBindings) {
  planBindings.forEach(({ plan, bindings }) => {
    let changed = false;
    ["study", "environment", "method"].forEach((kind) => {
      const binding = bindings[kind] || {};
      if (
        binding.source_kind !== "configured-filesystem-import"
        || !binding.package_id
        || !binding.id
      ) return;
      const entries = kind === "study"
        ? state.catalog.studies || []
        : kind === "environment"
        ? state.catalog.environments || []
        : state.catalog.methods || [];
      const replacement = entries.find((entry) => (
        entry.package_id === binding.package_id
        && entry.id === binding.id
        && entry.ref
        && entry.ref.source_kind === "realm-catalog"
      ));
      if (replacement && plan[kind] !== replacement) {
        plan[kind] = replacement;
        changed = true;
      }
    });
    if (!changed) return;
    const pair = selectedCompatibilityPair(plan);
    plan.checks = pair ? compatibilityChecks(pair) : [];
    if (!plan.study) plan.yaml = planYamlPreview(plan);
    if (plan.actionError && plan.actionError.title === "Publish package first") {
      plan.actionError = null;
    }
  });
}

function renderCatalogPackageFilter() {
  if (!els.componentPackageFilter) return;
  const packages = catalogPackageOptions();
  if (state.componentPackageFilter !== "all" && !packages.some((item) => item.id === state.componentPackageFilter)) {
    state.componentPackageFilter = "all";
  }
  const options = [
    { id: "all", label: "All packages" },
    ...packages,
  ];
  const html = options.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join("");
  if (els.componentPackageFilter.innerHTML !== html) {
    els.componentPackageFilter.innerHTML = html;
  }
  if (els.componentPackageFilter.value !== state.componentPackageFilter) {
    els.componentPackageFilter.value = state.componentPackageFilter;
  }
}

function catalogPackageOptions() {
  const counts = new Map();
  allComponents().forEach((component) => {
    const id = componentPackageId(component);
    if (!id) return;
    counts.set(id, (counts.get(id) || 0) + 1);
  });
  return Array.from(counts.entries())
    .sort((left, right) => left[0].localeCompare(right[0]))
    .map(([id, count]) => ({ id, label: `${id} (${count})` }));
}

function componentPackageId(component) {
  const entry = component && component.entry || {};
  return String(entry.package_id || entry.package || "unpackaged");
}

function authoredInterfaceProfiles(iface) {
  if (!iface || typeof iface !== "object" || Array.isArray(iface)) return [];
  const profiles = Array.isArray(iface.launchProfiles)
    ? iface.launchProfiles
    : [{ ...iface, id: "default" }];
  return profiles
    .filter((profile) => profile && typeof profile === "object" && !Array.isArray(profile))
    .map((profile, index) => ({
      ...profile,
      id: String(profile.id || (profiles.length === 1 ? "default" : `profile-${index + 1}`)),
    }))
    .filter((profile) => Array.isArray(profile.command)
      && profile.command.length
      && profile.presentation
      && Number(profile.presentation.port) > 0);
}

function summarizedInterfaceProfiles(iface) {
  if (!iface || typeof iface !== "object" || !Array.isArray(iface.profiles)) return [];
  return iface.profiles.filter((profile) => profile && profile.id && profile.presentation && Number(profile.presentation.port) > 0);
}

function selectedInterfaceProfile(profiles, selectionKey, defaultProfileId = "") {
  if (!profiles.length) return null;
  const selectedId = state.interfaceProfileSelections[selectionKey];
  const selected = profiles.find((profile) => profile.id === selectedId);
  if (selected) return selected;
  const fallback = profiles.find((profile) => profile.id === defaultProfileId)
    || profiles.find((profile) => profile.id === "default")
    || profiles[0];
  state.interfaceProfileSelections[selectionKey] = fallback.id;
  return fallback;
}

function componentInterfaceProfiles(component) {
  return summarizedInterfaceProfiles(component.entry && component.entry.interface);
}

function componentSelectedInterfaceProfile(component) {
  const profiles = componentInterfaceProfiles(component);
  const summary = component.entry && component.entry.interface || {};
  return selectedInterfaceProfile(profiles, componentLaunchKey(component), summary.defaultProfileId || "");
}

function interfaceProfileLaunchCapability(profile, iface = null) {
  const profileAction = profile && profile.launch;
  if (profileAction && typeof profileAction === "object") return profileAction;
  const summaryAction = iface && iface.actions && iface.actions.launch;
  if (summaryAction && typeof summaryAction === "object") return summaryAction;
  return {
    supported: true,
    eligible: true,
    code: "ready",
    reason: "Open this interface in an isolated launch runtime.",
  };
}

function componentInterfaceLaunchCapability(component, profile = null) {
  const iface = component && component.entry && component.entry.interface || {};
  return interfaceProfileLaunchCapability(
    profile || componentSelectedInterfaceProfile(component),
    iface,
  );
}

function catalogComponentAction(component) {
  return state.catalogComponentActions[componentLaunchKey(component)] || null;
}

function catalogComponentActionStatus(component) {
  const action = catalogComponentAction(component);
  if (!action) return "";
  const label = action.mode === "edit" ? "editable Workspace" : "read-only source";
  if (action.pending) {
    return `
      <section class="component-action-status" role="status" aria-live="polite">
        <strong>Opening ${escapeHtml(label)}…</strong>
        <p>Stay on this Catalog item while Studio prepares it.</p>
      </section>
    `;
  }
  if (!action.error) return "";
  return `
    <section class="component-action-status component-action-failed" role="alert">
      <strong>${escapeHtml(action.mode === "edit" ? "Workspace could not be opened" : "Source could not be opened")}</strong>
      <p>${escapeHtml(action.error)}</p>
      <small>Use the action above to try again.</small>
    </section>
  `;
}

function renderComponentDetail() {
  const component = componentByKey(state.selectedComponentKey);
  if (!component) {
    els.componentDetail.innerHTML = emptyState("Select a Catalog item.");
    return;
  }
  const item = component.entry;
  const summary = item.summary || {};
  const activeProfiles = componentInterfaceProfiles(component);
  const activeInterface = componentSelectedInterfaceProfile(component);
  const hasInterface = Boolean(activeInterface);
  const interfaceCapability = hasInterface
    ? componentInterfaceLaunchCapability(component, activeInterface)
    : null;
  const interfaceUnavailable = Boolean(
    interfaceCapability && interfaceCapability.eligible !== true,
  );
  const launchState = hasInterface && state.interfaceLaunch && state.interfaceLaunch.key === componentLaunchKey(component)
    ? state.interfaceLaunch
    : null;
  const interfaceFailed = Boolean(launchState && launchState.status === "failed");
  const interfaceReason = interfaceUnavailable
    ? String(interfaceCapability.reason || "This interface is unavailable.")
    : "";
  const interfaceAction = hasInterface
    ? `<button class="ghost-button component-launch-interface" type="button">${launchState && launchState.status === "ready" ? "Open running interface" : interfaceFailed ? "Try interface again" : launchState ? "View interface progress" : "Open interface"}</button>`
    : "";
  const interfaceGuidance = interfaceReason
    ? `<p class="source-note component-interface-guidance">${escapeHtml(interfaceReason)}</p>`
    : "";
  const componentAction = catalogComponentAction(component);
  const componentActionPending = Boolean(componentAction && componentAction.pending);
  const componentActionStatus = catalogComponentActionStatus(component);
  const editableCapability = componentEditableWorkspaceCapability(component);
  const editDisabled = editableCapability.eligible !== true || componentActionPending;
  const editReason = String(editableCapability.reason || "");
  const linkedWorkspaceId = String(editableCapability.workspace_id || "");
  const editRetry = Boolean(componentAction && componentAction.mode === "edit" && componentAction.error);
  const inspectPending = Boolean(componentActionPending && componentAction.mode === "inspect");
  const inspectRetry = Boolean(componentAction && componentAction.mode === "inspect" && componentAction.error);
  const editLabel = componentActionPending && componentAction.mode === "edit"
    ? "Opening…"
    : editRetry
    ? "Try opening Workspace again"
    : linkedWorkspaceId
    ? "Open Workspace"
    : "Edit in Workspace";
  const inspectLabel = inspectPending ? "Opening…" : inspectRetry ? "Try opening source again" : "View source";
  const editButton = `<button class="ghost-button component-edit" type="button" ${editDisabled ? `disabled title="${escapeHtml(editReason)}"` : ""}>${escapeHtml(editLabel)}</button>`;
  const editGuidance = editableCapability.eligible !== true && editReason
    ? `<p class="source-note component-edit-guidance">${escapeHtml(editReason)}</p>`
    : "";
  if (component.kind === "resource") {
    els.componentDetail.innerHTML = `
      ${entityHeader(item, component.kind)}
      <div class="action-row">
        <button class="ghost-button component-inspect" type="button" ${componentActionPending ? "disabled" : ""}>${escapeHtml(inspectLabel)}</button>
        ${editButton}
        ${interfaceAction}
      </div>
      ${interfaceGuidance}
      ${editGuidance}
      ${componentActionStatus}
      <div class="detail-grid">
        ${kvPanel("Resource", [
          ["Purpose", resourcePurposeLabel(item)],
          ["Files", summary.file_count ?? "-"],
          ["README", summary.readme || "-"],
          ["Mode", "read-only Catalog item"],
          ["Source config", componentConfigSource(component)],
        ])}
        ${kvPanel("Use", [
          ["Source", "View read-only without creating a Workspace"],
          ["Editing", "Create or reopen an editable Workspace"],
          ["Interface", hasInterface ? `${activeProfiles.length} profile${activeProfiles.length === 1 ? "" : "s"}; port ${activeInterface.presentation.port}` : "not declared"],
        ])}
      </div>
      ${componentGuidePanel(component)}
      ${componentEnvRequirementsPanel(item.raw_config || {})}
    `;
    els.componentDetail.querySelector(".component-inspect").addEventListener("click", () => openComponentSession(component, "inspect"));
    els.componentDetail.querySelector(".component-edit").addEventListener("click", () => openCatalogEditableWorkspace(component));
    const launchButton = els.componentDetail.querySelector(".component-launch-interface");
    if (launchButton) launchButton.addEventListener("click", () => openComponentInterface(component));
    bindComponentReadOnlyControls();
    return;
  }
  const pairs = component.kind === "environment"
    ? compatibleMethodsForEnvironment(item.uid)
    : compatibleEnvironmentsForMethod(item.uid);
  const studyActionReason = pairs.length
    ? ""
    : `No compatible ${component.kind === "environment" ? "Method" : "Environment"} is currently available.`;
  els.componentDetail.innerHTML = `
    ${entityHeader(item, component.kind)}
    <div class="action-row">
      <button class="primary-button component-use-study" type="button" ${pairs.length ? "" : `disabled title="${escapeHtml(studyActionReason)}"`}>Use in new Study</button>
      <button class="ghost-button component-inspect" type="button" ${componentActionPending ? "disabled" : ""}>${escapeHtml(inspectLabel)}</button>
      ${editButton}
      ${interfaceAction}
    </div>
    ${studyActionReason ? `<p class="source-note component-study-guidance">${escapeHtml(studyActionReason)}</p>` : ""}
    ${interfaceGuidance}
    ${editGuidance}
    ${componentActionStatus}
    <div class="detail-grid">
      ${kvPanel("Contract", component.kind === "environment" ? [
        ["Candidate", summary.candidate_format],
        ["Metrics", (summary.metrics || []).join(", ") || "-"],
        ["Evaluator", summary.evaluate_type],
        ["Source config", componentConfigSource(component)],
      ] : [
        ["Accepts", (summary.candidate_formats || []).join(", ") || "-"],
        ["Protocol", summary.protocol],
        ["Implementation", summary.implementation_type],
        ["Source config", componentConfigSource(component)],
      ])}
      ${kvPanel("Runtime", component.kind === "environment" ? [
        ["Timeout", summary.runtime && summary.runtime.timeoutSeconds],
        ["Sandbox", summary.runtime && summary.runtime.sandbox],
        ["Interface", hasInterface ? `${activeProfiles.length} profile${activeProfiles.length === 1 ? "" : "s"}; port ${activeInterface.presentation.port}` : "not declared"],
      ] : [
        ["Runtime", summary.runtime && summary.runtime.type],
        ["Image", summary.runtime && summary.runtime.image],
        ["Interface", hasInterface ? `${activeProfiles.length} profile${activeProfiles.length === 1 ? "" : "s"}; port ${activeInterface.presentation.port}` : "not declared"],
      ])}
    </div>
    ${componentGuidePanel(component)}
    ${componentEnvRequirementsPanel(item.raw_config || {})}
    <div class="panel-section">
      <h3>Compatible ${component.kind === "environment" ? "Methods" : "Environments"}</h3>
      ${compatList(pairs, component.kind === "environment" ? "method" : "environment")}
    </div>
  `;
  els.componentDetail.querySelector(".component-inspect").addEventListener("click", () => openComponentSession(component, "inspect"));
  els.componentDetail.querySelector(".component-edit").addEventListener("click", () => openCatalogEditableWorkspace(component));
  els.componentDetail.querySelector(".component-use-study").addEventListener("click", () => {
    if (pairs[0]) createPlanFromPair(pairs[0]);
  });
  const launchButton = els.componentDetail.querySelector(".component-launch-interface");
  if (launchButton) launchButton.addEventListener("click", () => openComponentInterface(component));
  bindComponentReadOnlyControls();
  els.componentDetail.querySelectorAll("[data-build-study-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const pair = pairs[Number(button.dataset.buildStudyIndex)];
      if (pair) createPlanFromPair(pair);
    });
  });
}

function componentEditableWorkspaceCapability(component) {
  const actions = component && component.entry && component.entry.actions || {};
  const capability = actions.create_editable_workspace;
  if (capability && typeof capability === "object") return capability;
  return {
    eligible: false,
    code: "catalog_source_unpublished",
    reason: "Open the local source folder as a Workspace, then Check and publish it before creating an editable copy.",
  };
}

function componentConfigSource(component) {
  const item = component && component.entry || {};
  const summary = item.summary || {};
  const manifest = summary.manifest ? shortPath(`${item.path}/${summary.manifest}`) : "";
  const configPath = item.config_path || (item.config !== "resource" ? item.path : "");
  return manifest || (configPath ? shortPath(configPath) : "generated resource manifest");
}

function componentGuidePanel(component) {
  const rows = componentGuideRows(component);
  if (!rows.length) return "";
  return `
    <section class="panel-section component-guide-panel">
      <div class="component-guide-heading">
        <div>
          <h3>How this Catalog item works</h3>
          <p>${escapeHtml(componentGuideIntro(component.kind))}</p>
        </div>
      </div>
      <div class="component-guide-grid">
        ${rows.map((row) => `
          <div class="component-guide-item">
            <span>${escapeHtml(row.label)}</span>
            <strong>${escapeHtml(row.value || "-")}</strong>
            ${row.help ? `<small>${escapeHtml(row.help)}</small>` : ""}
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function componentGuideIntro(kind) {
  if (kind === "environment") return "An environment owns evaluation: it defines what candidates look like, how trials run, and which metrics come back.";
  if (kind === "method") return "A method owns proposal: it reads the environment context and returns candidates that match compatible environments.";
  return "A resource is supporting material, such as a helper interface, dataset, article, or reference workspace. It is not evaluated as a study component by itself.";
}

function componentGuideRows(component) {
  const raw = component.entry && component.entry.raw_config || {};
  const summary = component.entry && component.entry.summary || {};
  if (component.kind === "environment") {
    return [
      {
        label: "Candidate contract",
        value: raw.candidate && raw.candidate.format || summary.candidate_format || "not declared",
        help: raw.candidate && raw.candidate.description || "The format a compatible method must produce.",
      },
      {
        label: "Metrics",
        value: listPreview(raw.metrics && raw.metrics.keys || summary.metrics),
        help: "The names that can be selected as study objectives or secondary metrics.",
      },
      {
        label: "Evaluator",
        value: evaluatorSummary(raw.evaluator) || summary.evaluate_type || "not declared",
        help: "The entrypoint OptPilot calls for each candidate trial.",
      },
      {
        label: "Method context",
        value: methodContextSummary(raw.methodContext),
        help: "Environment-owned files or instructions visible to compatible methods.",
      },
      {
        label: "Runtime",
        value: componentExecutionSummary(raw),
        help: "Execution and dependency setup declared by this environment.",
      },
    ];
  }
  if (component.kind === "method") {
    return [
      {
        label: "Accepts",
        value: listPreview(raw.accepts && raw.accepts.formats || summary.candidate_formats),
        help: "Candidate formats this method can propose.",
      },
      {
        label: "Entrypoint",
        value: entrypointSummary(raw.entrypoint) || summary.implementation_type || "not declared",
        help: "How OptPilot invokes the method.",
      },
      {
        label: "Required context",
        value: listPreview(raw.accepts && raw.accepts.requires && raw.accepts.requires.context || summary.required_context),
        help: "Environment context paths this method expects.",
      },
      {
        label: "Required capabilities",
        value: listPreview(raw.accepts && raw.accepts.requires && raw.accepts.requires.capabilities || summary.required_capabilities),
        help: "Environment capabilities this method expects.",
      },
      {
        label: "Runtime",
        value: componentExecutionSummary(raw),
        help: "Execution and dependency setup declared by this method.",
      },
    ];
  }
  return [
    {
      label: "Purpose",
      value: resourcePurposeLabel(component.entry),
      help: raw.purpose
        ? "This role is explicitly declared by the Resource manifest."
        : "No role is declared, so Studio uses the honest Resource fallback.",
    },
    {
      label: "Files",
      value: resourceFileSummary(raw.files, summary),
      help: "Files included with this resource package.",
    },
    {
      label: "Interface",
      value: interfaceProfileSummary(raw.interface),
      help: "Typed launch profiles for an optional GUI or helper service.",
    },
    {
      label: "Setup",
      value: interfaceSetupSummary(raw.interface),
      help: "Profile-specific install steps declared under interface.runtime.setup.",
    },
  ];
}

function listPreview(value) {
  const items = []
    .concat(value || [])
    .map((item) => String(item || "").trim())
    .filter(Boolean);
  if (!items.length) return "not declared";
  if (items.length <= 4) return items.join(", ");
  return `${items.slice(0, 4).join(", ")} +${items.length - 4} more`;
}

function evaluatorSummary(evaluator) {
  if (!evaluator || typeof evaluator !== "object") return "";
  if (evaluator.python) return `python: ${evaluator.python}`;
  if (evaluator.command) return "command";
  if (evaluator.adapter) return `adapter: ${evaluator.adapter}`;
  return "";
}

function entrypointSummary(entrypoint) {
  if (!entrypoint || typeof entrypoint !== "object") return "";
  const protocol = entrypoint.protocol ? ` (${entrypoint.protocol})` : "";
  if (entrypoint.python) return `python: ${entrypoint.python}${protocol}`;
  if (entrypoint.command) return `command${protocol}`;
  return protocol.replace(/[()]/g, "");
}

function methodContextSummary(methodContext) {
  if (!methodContext || typeof methodContext !== "object") return "not declared";
  const references = Array.isArray(methodContext.references) ? methodContext.references.length : 0;
  const instructions = Array.isArray(methodContext.instructions) ? methodContext.instructions.length : methodContext.instructions ? 1 : 0;
  const parts = [];
  if (references) parts.push(`${references} reference${references === 1 ? "" : "s"}`);
  if (instructions) parts.push(`${instructions} instruction${instructions === 1 ? "" : "s"}`);
  return parts.join(", ") || "not declared";
}

function componentExecutionSummary(raw = {}) {
  const runtime = raw.runtime && typeof raw.runtime === "object" ? raw.runtime : {};
  const parts = [];
  const sandbox = runtime.sandbox || "process";
  parts.push(`${sandbox} runtime`);
  const runtimeSetup = setupStepCount(runtime.setup);
  if (runtimeSetup) parts.push(`${runtimeSetup} runtime setup step${runtimeSetup === 1 ? "" : "s"}`);
  if (runtime.container && typeof runtime.container === "object") {
    const image = runtime.container.image || runtime.container.build && runtime.container.build.tag;
    if (image) parts.push(`container ${image}`);
  }
  const profiles = authoredInterfaceProfiles(raw.interface);
  const interfaceSetup = profiles.reduce((count, profile) => count + setupStepCount(profile.runtime && profile.runtime.setup), 0);
  if (interfaceSetup) parts.push(`${interfaceSetup} interface setup step${interfaceSetup === 1 ? "" : "s"}`);
  if (profiles.length) parts.push(`${profiles.length} interface profile${profiles.length === 1 ? "" : "s"}`);
  if (parts.length === 1 && !raw.runtime && !raw.interface) return "process runtime defaults; no setup declared";
  return parts.join("; ");
}

function interfaceSetupSummary(iface = {}) {
  const profiles = authoredInterfaceProfiles(iface);
  const steps = profiles.reduce((count, profile) => count + setupStepCount(profile.runtime && profile.runtime.setup), 0);
  if (steps) return `${steps} setup step${steps === 1 ? "" : "s"}`;
  return "not declared";
}

function interfaceProfileSummary(iface = {}) {
  const profiles = authoredInterfaceProfiles(iface);
  if (!profiles.length) return "not declared";
  return `${profiles.length} profile${profiles.length === 1 ? "" : "s"}: ${profiles.map((profile) => `${profile.id} (${profile.presentation.port})`).join(", ")}`;
}

function setupStepCount(setup) {
  return setup && Array.isArray(setup.steps) ? setup.steps.length : 0;
}

function resourceFileSummary(files, summary = {}) {
  if (Array.isArray(files)) return files.length ? `${files.length} file${files.length === 1 ? "" : "s"}` : "not declared";
  if (files && typeof files === "object") return listPreview(Object.keys(files));
  if (summary.file_count !== undefined) return `${summary.file_count} file${summary.file_count === 1 ? "" : "s"}`;
  return "not declared";
}

function componentEnvRequirementsPanel(raw = {}) {
  const requirements = componentEnvRequirements(raw);
  if (!requirements.length) return "";
  const configured = configuredEnvironmentVariableNames();
  return `
    <div class="env-requirements-panel">
      <div>
        <strong>Environment variables</strong>
        <p>These names are declared by runtime envFromHost or interface grants. Studio injects only declared variables.</p>
      </div>
      <div class="env-requirements-list">
        ${requirements.map((item) => {
          const isConfigured = configured.has(item.name);
          return `
            <div class="env-requirement-row">
              <span>
                <strong>${escapeHtml(item.name)}</strong>
                <small>${escapeHtml(item.phase)} · ${escapeHtml(item.path)}</small>
              </span>
              ${statusPill(isConfigured ? "configured" : "missing")}
            </div>
          `;
        }).join("")}
      </div>
      <button class="ghost-button open-settings-from-env" type="button">Open Studio Settings</button>
    </div>
  `;
}

function componentEnvRequirements(raw = {}) {
  const requirements = [];
  const add = (phase, path, names) => {
    envNameList(names).forEach((name) => {
      requirements.push({ phase, path, name });
    });
  };
  const runtime = raw.runtime && typeof raw.runtime === "object" ? raw.runtime : {};
  const runtimeSetup = runtime.setup && typeof runtime.setup === "object" ? runtime.setup : {};
  add("Runtime setup", "runtime.setup.envFromHost", runtimeSetup.envFromHost);
  add("Runtime execution", "runtime.envFromHost", runtime.envFromHost);
  authoredInterfaceProfiles(raw.interface).forEach((profile) => {
    const prefix = raw.interface && Array.isArray(raw.interface.launchProfiles)
      ? `interface.launchProfiles.${profile.id}`
      : "interface";
    add("Interface environment", `${prefix}.grants.envFromHost`, profile.grants && profile.grants.envFromHost);
    add("Interface secrets", `${prefix}.grants.secretsFromHost`, profile.grants && profile.grants.secretsFromHost);
  });
  const seen = new Set();
  return requirements.filter((item) => {
    const key = `${item.phase}:${item.path}:${item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function envNameList(value) {
  return []
    .concat(Array.isArray(value) ? value : [])
    .map((item) => String(item || "").trim())
    .filter(Boolean);
}

function bindComponentReadOnlyControls() {
  els.componentDetail.querySelectorAll(".open-settings-from-env").forEach((button) => {
    button.addEventListener("click", () => openSettings({ tab: "environment" }));
  });
}

function componentLaunchKey(component) {
  if (!component) return "";
  return `${component.kind}:${component.entry && component.entry.uid || component.key}`;
}

function interfaceLaunchTimestampMs(value) {
  if (value === null || value === undefined || value === "" || value === 0) return 0;
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric < 1e12 ? numeric * 1000 : numeric;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function interfaceLaunchActivityHtml(launchState, currentStep) {
  const startedAt = interfaceLaunchTimestampMs(launchState.started_at || launchState.startedAt) || Date.now();
  const stepAt = interfaceLaunchTimestampMs(currentStep && currentStep.time);
  const updatedAt = interfaceLaunchTimestampMs(launchState.updated_at) || stepAt || startedAt;
  const elapsed = formatDuration(Math.max(0, Date.now() - startedAt)) || "<1s";
  const quietFor = formatDuration(Math.max(0, Date.now() - updatedAt)) || "<1s";
  const stage = currentStep && currentStep.title || (launchState.status === "queued" ? "Waiting to start" : "Starting interface");
  return `
    <p class="interface-launch-current-stage"><span>Current stage</span> ${escapeHtml(stage)}</p>
    <p class="interface-launch-activity">
      <span data-interface-launch-elapsed data-started-at="${escapeHtml(String(startedAt))}">Elapsed ${escapeHtml(elapsed)}</span>
      <span aria-hidden="true">·</span>
      <span data-interface-launch-last-activity data-updated-at="${escapeHtml(String(updatedAt))}">Last activity ${escapeHtml(quietFor)} ago</span>
    </p>
  `;
}

function refreshInterfaceLaunchActivity() {
  document.querySelectorAll("[data-interface-launch-elapsed]").forEach((element) => {
    const startedAt = Number(element.dataset.startedAt || 0);
    if (startedAt > 0) element.textContent = `Elapsed ${formatDuration(Math.max(0, Date.now() - startedAt)) || "<1s"}`;
  });
  document.querySelectorAll("[data-interface-launch-last-activity]").forEach((element) => {
    const updatedAt = Number(element.dataset.updatedAt || 0);
    if (updatedAt > 0) element.textContent = `Last activity ${formatDuration(Math.max(0, Date.now() - updatedAt)) || "<1s"} ago`;
  });
}

function syncInterfaceLaunchActivity(launchState, root = null) {
  if (!launchState) return;
  const startedAt = interfaceLaunchTimestampMs(launchState.started_at || launchState.startedAt);
  const steps = Array.isArray(launchState.steps) ? launchState.steps : [];
  const updatedAt = interfaceLaunchTimestampMs(launchState.updated_at)
    || interfaceLaunchTimestampMs(steps.length && steps[steps.length - 1].time)
    || startedAt;
  const scope = root || document;
  scope.querySelectorAll("[data-interface-launch-elapsed]").forEach((element) => {
    if (startedAt > 0) element.dataset.startedAt = String(startedAt);
  });
  scope.querySelectorAll("[data-interface-launch-last-activity]").forEach((element) => {
    if (updatedAt > 0) element.dataset.updatedAt = String(updatedAt);
  });
  refreshInterfaceLaunchActivity();
}

function interfaceLaunchTitle(label, status) {
  if (status === "ready") return `${label} is running`;
  if (status === "stopping") return `Stopping ${label}`;
  if (status === "cleanup_pending") return `${label} cleanup needs attention`;
  if (status === "failed") return `${label} could not start`;
  if (status === "stopped") return `${label} stopped`;
  return `Preparing ${label}`;
}

function interfaceLaunchStateClass(status) {
  const normalized = String(status || "queued").toLowerCase();
  if (normalized === "ready") return "ready";
  if (normalized === "failed") return "failed";
  if (normalized === "cleanup_pending") return "warning";
  if (normalized === "stopping" || normalized === "stopped") return "stopped";
  return "running";
}

function interfaceLaunchSummaryStageHtml(launchState, currentStep) {
  const status = String(launchState && launchState.status || "");
  const startedAt = interfaceLaunchTimestampMs(
    launchState && (launchState.started_at || launchState.startedAt),
  ) || Date.now();
  const elapsed = formatDuration(Math.max(0, Date.now() - startedAt)) || "<1s";
  const stage = status === "ready"
    ? "Ready"
    : status === "failed"
    ? "Attention required"
    : status === "cleanup_pending"
    ? "Cleanup required"
    : status === "stopping"
    ? "Stopping"
    : currentStep && currentStep.title || (status === "queued" ? "Waiting to start" : "Starting");
  return `
    <span class="interface-launch-summary-stage">
      ${escapeHtml(stage)}
      <span aria-hidden="true">·</span>
      <span data-interface-launch-elapsed data-started-at="${escapeHtml(String(startedAt))}">Elapsed ${escapeHtml(elapsed)}</span>
    </span>
  `;
}

function interfaceOutputRecoveryState(launchState) {
  const action = launchState && launchState.actions && launchState.actions.capture_output_tree;
  if (!action || typeof action !== "object") return "missing";
  return action.supported === false ? "unsupported" : "available";
}

function renderInterfaceOutputRecovery(launchState) {
  if (!launchState || !launchState.outputs_enabled) return "";
  const action = launchState.actions && launchState.actions.capture_output_tree;
  if (!action || typeof action !== "object" || action.supported === false) return "";
  return `
    <details class="interface-output-more interface-output-recovery">
      <summary>Add a missing output</summary>
      <div class="interface-output-picker-slot" data-picker-signature="${escapeHtml(interfaceOutputPickerRenderSignature(launchState))}">
        ${renderInterfaceOutputTreePicker(launchState)}
      </div>
    </details>
  `;
}

function compactInterfaceLaunchStatus({
  launchState,
  label,
  detail,
  steps,
  currentStep,
  logText,
  errorDetail,
  lifecycleActions,
}) {
  const status = String(launchState && launchState.status || "queued");
  const failed = status === "failed";
  const cleanupPending = status === "cleanup_pending";
  const outputs = launchState && launchState.result && Array.isArray(launchState.result.outputs)
    ? launchState.result.outputs
    : [];
  const outputFailure = outputs.some((output) => String(output && output.status || "").toLowerCase() === "failed");
  const defaultPanel = failed || cleanupPending
    ? "details"
    : outputFailure
    ? "outputs"
    : "";
  const domKey = String(launchState && (launchState.launch_id || launchState.key) || "pending")
    .replace(/[^a-zA-Z0-9_-]+/g, "-");
  const detailsId = `interface-launch-details-${domKey}`;
  const outputsId = `interface-launch-outputs-${domKey}`;
  const errorText = failed
    ? String(launchState.error || detail || "Studio could not start this interface.")
    : "";
  const hasVisibleError = Boolean(errorText || errorDetail || launchState.stop_error);
  const recovery = outputs.length ? "" : renderInterfaceOutputRecovery(launchState);
  return `
    <section
      class="interface-launch-status interface-launch-compact ${failed ? "interface-launch-failed" : ""}"
      data-interface-launch-status="${escapeHtml(status)}"
      data-interface-output-recovery-state="${escapeHtml(interfaceOutputRecoveryState(launchState))}"
    >
      <div class="interface-launch-summary-row">
        <div class="interface-launch-summary-copy">
          <span class="interface-launch-state" role="status" aria-live="polite">
            <span class="interface-launch-state-dot ${escapeHtml(interfaceLaunchStateClass(status))}" aria-hidden="true"></span>
            <strong>${escapeHtml(interfaceLaunchTitle(label, status))}</strong>
          </span>
          ${interfaceLaunchSummaryStageHtml(launchState, currentStep)}
        </div>
        <div class="action-row interface-launch-summary-actions">
          <button
            class="ghost-button interface-launch-drawer-toggle"
            type="button"
            data-interface-drawer-toggle="details"
            aria-expanded="${defaultPanel === "details" ? "true" : "false"}"
            aria-controls="${escapeHtml(detailsId)}"
          >Launch details</button>
          ${outputs.length ? `
            <button
              class="ghost-button interface-launch-drawer-toggle interface-output-drawer-toggle"
              type="button"
              data-interface-drawer-toggle="outputs"
              aria-expanded="${defaultPanel === "outputs" ? "true" : "false"}"
              aria-controls="${escapeHtml(outputsId)}"
            >Outputs (${escapeHtml(String(outputs.length))})</button>
          ` : ""}
          ${lifecycleActions}
        </div>
      </div>
      ${hasVisibleError ? `
        <div class="interface-launch-visible-errors" role="alert">
          ${errorText ? `<p>${escapeHtml(errorText)}</p>` : ""}
          ${errorDetail ? `<p><strong>Last process error:</strong> ${escapeHtml(errorDetail)}</p>` : ""}
          ${launchState.stop_error ? `<p>${escapeHtml(launchState.stop_error)}</p>` : ""}
        </div>
      ` : ""}
      <div
        id="${escapeHtml(detailsId)}"
        class="interface-launch-drawer interface-launch-details"
        data-interface-drawer-panel="details"
        ${defaultPanel === "details" ? "" : "hidden"}
      >
        <p class="interface-launch-detail-copy">${escapeHtml(detail)}</p>
        ${interfaceLaunchActivityHtml(launchState, currentStep)}
        ${steps.length ? `
          <ol class="interface-launch-steps">
            ${steps.map((step) => `
              <li class="${escapeHtml(step.status || "running")}">
                <span>${escapeHtml(step.title || "Working")}</span>
              </li>
            `).join("")}
          </ol>
        ` : ""}
        ${logText ? `<details class="interface-launch-log-details"><summary>View log</summary><pre class="interface-launch-log">${escapeHtml(logText)}</pre></details>` : ""}
        ${recovery}
      </div>
      ${renderInterfaceOutputs(launchState, {
        panelId: outputsId,
        open: defaultPanel === "outputs",
      })}
    </section>
  `;
}

function interfaceLaunchStatus(component, launchState) {
  const profile = componentInterfaceProfiles(component).find((item) => item.id === launchState.profile_id)
    || componentSelectedInterfaceProfile(component)
    || {};
  const port = profile.presentation && profile.presentation.port || launchState.port || "-";
  const label = profile.label || launchState.label || "interface";
  const steps = (launchState.steps || []).slice(-6);
  const currentStep = steps[steps.length - 1];
  const logs = launchState.logs || {};
  const logText = [logs.stdout, logs.stderr].filter(Boolean).join("\n").trim();
  const ready = launchState.status === "ready";
  const cleanupPending = launchState.status === "cleanup_pending";
  const stopping = launchState.status === "stopping";
  const failed = launchState.status === "failed";
  const errorDetail = failed ? String(launchState.error_detail || "").trim() : "";
  const canStop = Boolean(launchState.launch_id) && launchState.can_stop !== false;
  const detail = failed
    ? launchState.error || "Studio could not start this interface."
    : ready
    ? "The interface is running from the exact published Catalog version."
    : cleanupPending
    ? "Execution stopped, but launch-scoped cleanup still needs to be retried."
    : currentStep && currentStep.detail || `Mounting the Catalog source read-only, starting an isolated runtime, and waiting for port ${port}.`;
  const lifecycleActions = failed
    ? `<button class="primary-button component-retry-interface" type="button">Try again</button>`
    : canStop
    ? `<button class="${cleanupPending ? "primary-button" : "ghost-button"} component-stop-interface" type="button" ${stopping ? "disabled" : ""}>${cleanupPending ? "Retry cleanup" : stopping ? "Stopping…" : "Stop interface"}</button>`
    : "";
  return compactInterfaceLaunchStatus({
    launchState,
    label,
    detail,
    steps,
    currentStep,
    logText,
    errorDetail,
    lifecycleActions,
  });
}

function workspaceInterfaceLaunchStatus(session, launchState) {
  const label = launchState.label || "interface";
  const steps = (launchState.steps || []).slice(-4);
  const currentStep = steps[steps.length - 1];
  const logs = launchState.logs || {};
  const logText = [logs.stdout, logs.stderr].filter(Boolean).join("\n").trim();
  const ready = launchState.status === "ready";
  const cleanupPending = launchState.status === "cleanup_pending";
  const stopping = launchState.status === "stopping";
  const failed = launchState.status === "failed";
  const errorDetail = failed ? String(launchState.error_detail || "").trim() : "";
  const fallback = ready
    ? `The interface is using the current files in ${session.title}.`
    : cleanupPending
    ? "Execution has stopped, but launch-scoped cleanup still needs to be reconciled."
    : failed
    ? launchState.error || "Studio could not start this interface."
    : "Starting a separate launch runtime over this existing editable workspace.";
  const detail = failed ? fallback : currentStep && currentStep.detail || fallback;
  const canStop = Boolean(launchState.launch_id) && launchState.can_stop !== false;
  const lifecycleActions = failed
    ? `<button class="primary-button workspace-retry-interface" type="button">Try again</button>`
    : canStop
    ? `<button class="${cleanupPending ? "primary-button" : "ghost-button"} workspace-stop-interface" type="button" ${stopping ? "disabled" : ""}>${cleanupPending ? "Retry cleanup" : stopping ? "Stopping…" : "Stop interface"}</button>`
    : "";
  return compactInterfaceLaunchStatus({
    launchState,
    label,
    detail,
    steps,
    currentStep,
    logText,
    errorDetail,
    lifecycleActions,
  });
}

function interfaceOutputStatusLabel(status) {
  const normalized = String(status || "sealing").toLowerCase();
  if (normalized === "ready") return "Ready";
  if (normalized === "failed") return "Failed";
  return "Preparing";
}

function renderInterfaceOutputs(launchState, options = {}) {
  const outputs = launchState && launchState.result && Array.isArray(launchState.result.outputs)
    ? launchState.result.outputs
    : [];
  if (!outputs.length) return "";
  const signature = interfaceOutputRenderSignature(launchState);
  return `
    <div
      id="${escapeHtml(options.panelId || "interface-launch-outputs")}"
      class="interface-launch-drawer interface-output-section"
      aria-label="Outputs"
      data-interface-drawer-panel="outputs"
      data-output-signature="${escapeHtml(signature)}"
      data-output-failure="${outputs.some((output) => String(output && output.status || "").toLowerCase() === "failed") ? "true" : "false"}"
      data-output-recovery-state="${escapeHtml(interfaceOutputRecoveryState(launchState))}"
      ${options.open ? "" : "hidden"}
    >
      <div class="interface-output-heading">
        <div>
          <strong>Outputs</strong>
          <span>Results from this interface are temporary. Save a folder as a Workspace to keep editing it.</span>
        </div>
      </div>
      <div class="interface-output-list">
        ${renderInterfaceOutputList(outputs)}
      </div>
      ${renderInterfaceOutputRecovery(launchState)}
    </div>
  `;
}

function renderInterfaceOutputList(outputs) {
  return outputs.length
    ? outputs.map(renderInterfaceOutputCard).join("")
    : `<p class="interface-output-empty">No outputs have been reported yet.</p>`;
}

function captureInterfaceLaunchDisclosureState(surface) {
  const launchStatus = surface && surface.querySelector(".interface-launch-status");
  if (!launchStatus) return null;
  const openPanel = Array.from(
    launchStatus.querySelectorAll("[data-interface-drawer-panel]"),
  ).find((panel) => !panel.hidden);
  const log = launchStatus.querySelector(".interface-launch-log-details");
  const recovery = launchStatus.querySelector(".interface-output-recovery");
  const outputPanel = launchStatus.querySelector('[data-interface-drawer-panel="outputs"]');
  return {
    launchStatus: String(launchStatus.dataset.interfaceLaunchStatus || ""),
    openPanel: openPanel ? String(openPanel.dataset.interfaceDrawerPanel || "") : "",
    logOpen: Boolean(log && log.open),
    recoveryOpen: Boolean(recovery && recovery.open),
    outputFailure: Boolean(outputPanel && outputPanel.dataset.outputFailure === "true"),
  };
}

function setInterfaceLaunchDrawer(surface, name) {
  if (!surface) return;
  const requested = String(name || "");
  surface.querySelectorAll("[data-interface-drawer-panel]").forEach((panel) => {
    panel.hidden = String(panel.dataset.interfaceDrawerPanel || "") !== requested;
  });
  surface.querySelectorAll("[data-interface-drawer-toggle]").forEach((button) => {
    button.setAttribute(
      "aria-expanded",
      String(button.dataset.interfaceDrawerToggle || "") === requested ? "true" : "false",
    );
  });
}

function restoreInterfaceLaunchDisclosureState(surface, previous, launchState) {
  if (!surface || !previous) return;
  const status = String(launchState && launchState.status || "");
  const outputPanel = surface.querySelector('[data-interface-drawer-panel="outputs"]');
  const outputFailure = Boolean(outputPanel && outputPanel.dataset.outputFailure === "true");
  let panel = previous.openPanel;
  if (
    ["failed", "cleanup_pending"].includes(status)
    && previous.launchStatus !== status
  ) {
    panel = "details";
  } else if (outputFailure && !previous.outputFailure) {
    panel = "outputs";
  }
  if (panel === "outputs" && !outputPanel) panel = "";
  setInterfaceLaunchDrawer(surface, panel);
  const log = surface.querySelector(".interface-launch-log-details");
  if (log) log.open = previous.logOpen;
  const recovery = surface.querySelector(".interface-output-recovery");
  if (recovery) recovery.open = previous.recoveryOpen;
}

function bindInterfaceLaunchDisclosureControls(root) {
  if (!root) return;
  root.querySelectorAll("[data-interface-drawer-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = String(button.dataset.interfaceDrawerToggle || "");
      const expanded = button.getAttribute("aria-expanded") === "true";
      setInterfaceLaunchDrawer(root, expanded ? "" : panel);
    });
  });
}

function interfaceOutputRenderSignature(launchState) {
  const outputs = launchState && launchState.result && Array.isArray(launchState.result.outputs)
    ? launchState.result.outputs
    : [];
  const picker = launchState && launchState.actions && launchState.actions.capture_output_tree || {};
  return JSON.stringify({ outputs, picker });
}

function interfaceOutputPickerRenderSignature(launchState) {
  const picker = launchState && launchState.actions && launchState.actions.capture_output_tree || {};
  return JSON.stringify([
    picker.supported === true ? true : picker.supported === false ? false : null,
    picker.eligible === true ? true : picker.eligible === false ? false : null,
    picker.code || "",
    picker.reason || "",
    picker.kind || "",
  ]);
}

function renderInterfaceOutputTreePicker(launchState) {
  const action = launchState && launchState.actions && launchState.actions.capture_output_tree;
  if (!action || typeof action !== "object") return "";
  if (!action.eligible) {
    return `<p class="interface-output-guidance interface-output-picker-unavailable">Manual output selection is unavailable: ${escapeHtml(action.reason || action.code || "This interface has no active output folder.")}</p>`;
  }
  return `
    <form class="interface-output-tree-picker">
      <p class="interface-output-guidance">Output cards normally appear when the running interface reports a completed result. If it wrote a completed folder but no card appeared, add it here.</p>
      <div class="interface-output-picker-fields">
        <label class="control-field">
          <span class="control-label"><strong>Folder to add</strong><small>Choose only the completed result folder.</small></span>
          <select name="path" disabled><option value=".">Loading folders...</option></select>
        </label>
        <label class="control-field">
          <span class="control-label"><strong>Output name</strong><small>Shown on the output card and used as the Workspace name if you save it.</small></span>
          <input name="label" type="text" maxlength="512" value="Generated output" required />
        </label>
      </div>
      <div class="action-row interface-output-picker-actions">
        <button class="ghost-button interface-output-tree-refresh" type="button">Check folders again</button>
        <button class="primary-button interface-output-tree-capture" type="submit" disabled>Add output</button>
      </div>
      <p class="interface-output-guidance">This adds a read-only output card. It does not create a Workspace. Choose Save as Workspace on the card only if you want editable work.</p>
      <p class="interface-output-error interface-output-picker-error" role="alert"></p>
    </form>
  `;
}

function interfaceOutputExecutionStatusLabel(status) {
  const normalized = String(status || "queued").toLowerCase();
  if (normalized === "queued") return "Queued";
  if (normalized === "running") return "Running";
  if (normalized === "succeeded") return "Completed";
  if (normalized === "timed_out") return "Timed out";
  if (normalized === "cancelled") return "Cancelled";
  if (normalized === "rejected") return "Not run";
  if (normalized === "infrastructure_failed") return "Could not run";
  return "Failed";
}

function interfaceOutputArgumentDraftKey(outputId, actionId) {
  const launchId = String(state.interfaceLaunch && state.interfaceLaunch.launch_id || "");
  return `${launchId}\u0000${String(outputId || "")}\u0000${String(actionId || "")}`;
}

function rememberInterfaceOutputArgumentDraft(outputId, actionId, value) {
  const key = interfaceOutputArgumentDraftKey(outputId, actionId);
  state.interfaceOutputArgumentDrafts.set(key, String(value || "").slice(0, 32 * 1024));
  while (state.interfaceOutputArgumentDrafts.size > 128) {
    const first = state.interfaceOutputArgumentDrafts.keys().next().value;
    state.interfaceOutputArgumentDrafts.delete(first);
  }
}

function interfaceOutputArgumentsFromControl(button) {
  const control = button && button.closest(".interface-output-action-control");
  const input = control && control.querySelector(".interface-output-action-arguments");
  if (!input) return [];
  return String(input.value || "")
    .split(/\r?\n/)
    .filter((value) => value.length > 0);
}

function renderInterfaceOutputExecution(output) {
  const executions = Array.isArray(output && output.executions) ? output.executions : [];
  const latest = executions[0];
  if (!latest || typeof latest !== "object") return "";
  const result = latest.result && typeof latest.result === "object" ? latest.result : {};
  const status = String(result.status || latest.status || "queued").toLowerCase();
  const terminal = !["queued", "running"].includes(status);
  const duration = Number(result.duration_seconds);
  const durationLabel = Number.isFinite(duration) && duration > 0
    ? ` · ${formatDuration(duration * 1000)}`
    : "";
  const resultFiles = Array.isArray(result.result_files) ? result.result_files : [];
  const resultFileCount = Number.isFinite(Number(result.result_file_count))
    ? Number(result.result_file_count)
    : resultFiles.length;
  const stdout = String(result.stdout || "").trim();
  const stderr = String(result.stderr || "").trim();
  const log = [stdout, stderr].filter(Boolean).join(stdout && stderr ? "\n\n" : "");
  const failure = terminal && status !== "succeeded";
  return `
    <div class="interface-output-execution interface-output-execution-${escapeHtml(status)}" aria-live="polite">
      <div class="interface-output-execution-summary">
        <span class="interface-output-execution-dot" aria-hidden="true"></span>
        <strong>${escapeHtml(latest.label || "Output action")}</strong>
        <span>${escapeHtml(interfaceOutputExecutionStatusLabel(status))}${escapeHtml(durationLabel)}</span>
        ${resultFileCount ? `<span>${escapeHtml(`${resultFileCount} result ${resultFileCount === 1 ? "file" : "files"}`)}</span>` : ""}
      </div>
      ${failure && result.failure_code ? `<p class="interface-output-error">${escapeHtml(String(result.failure_code).replaceAll("_", " "))}</p>` : ""}
      ${log ? `
        <details class="interface-output-execution-log">
          <summary>View output</summary>
          <pre>${escapeHtml(log)}</pre>
        </details>
      ` : ""}
      ${resultFiles.length ? `
        <div class="interface-output-execution-files" aria-label="Result files">
          ${resultFiles.slice(0, 6).map((file) => {
            const path = String(file && file.path || "Result file");
            const access = file && file.access && typeof file.access === "object"
              ? file.access
              : {};
            return `
              <div class="interface-output-execution-file">
                <span title="${escapeHtml(path)}">${escapeHtml(path)}</span>
                ${access.preview_eligible && access.open_url
                  ? `<a href="${escapeHtml(access.open_url)}" target="_blank" rel="noopener">Open</a>`
                  : ""}
                ${access.eligible && access.download_url
                  ? `<a href="${escapeHtml(access.download_url)}">Download</a>`
                  : `<small>${escapeHtml(access.reason || "Unavailable")}</small>`}
              </div>
            `;
          }).join("")}
          ${resultFileCount > 6 ? `<small>${escapeHtml(`${resultFileCount - 6} more files are not shown here.`)}</small>` : ""}
        </div>
      ` : ""}
    </div>
  `;
}

function renderInterfaceOutputCard(output) {
  const outputId = String(output && output.id || "");
  const rawKind = String(output && output.kind || "output").toLowerCase();
  const kind = rawKind === "tree" ? "folder" : rawKind === "blob" ? "file" : rawKind;
  const status = String(output && output.status || "sealing").toLowerCase();
  const statusLabel = interfaceOutputStatusLabel(status);
  const actions = output && output.actions && typeof output.actions === "object" ? output.actions : {};
  const viewAction = actions.view_read_only && typeof actions.view_read_only === "object" ? actions.view_read_only : {};
  const keepAction = actions.keep_as_workspace && typeof actions.keep_as_workspace === "object" ? actions.keep_as_workspace : {};
  const retryAction = actions.retry_capture && typeof actions.retry_capture === "object" ? actions.retry_capture : {};
  const executeAction = actions.execute && typeof actions.execute === "object" ? actions.execute : {};
  const executeItems = Array.isArray(executeAction.items) ? executeAction.items : [];
  const eligibleExecuteItems = executeItems.filter((item) => item && item.eligible);
  const executePendingActionId = String(output && output.execute_pending_action_id || "");
  const activeExecutionActionIds = new Set(
    (Array.isArray(output && output.executions) ? output.executions : [])
      .filter((execution) => ["queued", "running"].includes(String(
        execution && execution.result && execution.result.status
        || execution && execution.status
        || "",
      ).toLowerCase()))
      .map((execution) => String(execution && execution.action_id || "")),
  );
  const executeUnavailable = status === "ready"
    && executeAction.supported === true
    && !executeAction.eligible;
  const viewEligible = Boolean(outputId) && Boolean(viewAction.eligible);
  const viewPending = Boolean(output && output.view_pending);
  const viewUnavailable = status === "ready"
    && Object.keys(viewAction).length > 0
    && !viewEligible;
  const keepEligible = Boolean(outputId) && Boolean(keepAction.eligible);
  const keptWorkspaceId = String(output && output.kept_workspace_id || "");
  const visibleStatusLabel = keptWorkspaceId
    ? "Saved"
    : status === "ready" && output && output.retained
    ? keepAction.supported === false
      ? "Saved result"
      : "Saved result · Ready as Workspace"
    : status === "ready" && keepAction.supported === false
    ? "Ready · Temporary"
    : status === "ready"
    ? "Ready to save · Temporary"
    : statusLabel;
  const keepState = String(output && output.keep_state || "not-started");
  const pending = Boolean(output && output.keep_pending) || keepState === "saving";
  const retryPending = Boolean(output && output.retry_pending);
  const logicalBytes = output && output.logical_bytes;
  const size = logicalBytes !== null && logicalBytes !== undefined && Number.isFinite(Number(logicalBytes)) && Number(logicalBytes) >= 0
    ? ` · ${formatBytes(logicalBytes)}`
    : "";
  const failed = status === "failed";
  const failureReason = failed && output && output.failure_reason
    ? String(output.failure_reason)
    : "Studio could not read and save this output. Check that generation is complete and the files are readable, then try again.";
  const keepUnsupported = status === "ready" && keepAction.supported === false;
  const keepReason = keepUnsupported && keepAction.reason ? String(keepAction.reason) : "";
  return `
    <article class="interface-output-card interface-output-${escapeHtml(status)}" data-interface-output-id="${escapeHtml(outputId)}">
      <div class="interface-output-card-heading">
        <div>
          <strong>${escapeHtml(output && output.label || outputId || "Output")}</strong>
          <span>${escapeHtml(kind)}${escapeHtml(size)}</span>
        </div>
        <span class="status-pill ${statusClass(statusLabel.toLowerCase())}">${escapeHtml(visibleStatusLabel)}</span>
      </div>
      ${status === "sealing" ? `<p>Recording a read-only snapshot of this output.</p>` : ""}
      ${failed ? `<p class="interface-output-error" role="alert">${escapeHtml(failureReason)}</p>` : ""}
      ${viewUnavailable ? `<p class="interface-output-guidance">${escapeHtml(viewAction.reason || "This saved result is not currently viewable.")}</p>` : ""}
      ${keepUnsupported ? `<p class="interface-output-guidance">${escapeHtml(keepReason || "This immutable output cannot be opened as an editable workspace.")}</p>` : ""}
      ${status === "ready" && keepAction.supported !== false && !keepAction.eligible ? `<p class="interface-output-guidance">${escapeHtml(keepAction.reason || "This output is not currently eligible to be saved as a Workspace.")}</p>` : ""}
      ${output && output.view_error ? `<p class="interface-output-error" role="alert">${escapeHtml(output.view_error)}</p>` : ""}
      ${output && output.keep_error ? `<p class="interface-output-error" role="alert">${escapeHtml(output.keep_error)}</p>` : ""}
      ${output && output.retry_error ? `<p class="interface-output-error" role="alert">${escapeHtml(output.retry_error)}</p>` : ""}
      ${output && output.execute_error ? `<p class="interface-output-error" role="alert">${escapeHtml(output.execute_error)}</p>` : ""}
      ${executeUnavailable ? `<p class="interface-output-guidance">${escapeHtml(executeAction.reason || "This output cannot be run right now.")}</p>` : ""}
      ${renderInterfaceOutputExecution(output)}
      ${failed && outputId && retryAction.eligible !== false ? `<div class="action-row interface-output-actions"><button class="ghost-button interface-output-retry" type="button" data-output-id="${escapeHtml(outputId)}" ${retryPending ? "disabled" : ""}>${retryPending ? "Retrying..." : "Try again"}</button></div>` : ""}
      ${viewEligible || keepEligible || eligibleExecuteItems.length ? `
        <div class="action-row interface-output-actions">
          ${eligibleExecuteItems.map((item) => {
            const actionId = String(item.id || "");
            const starting = executePendingActionId === actionId;
            const running = activeExecutionActionIds.has(actionId);
            const argumentDraft = state.interfaceOutputArgumentDrafts.get(
              interfaceOutputArgumentDraftKey(outputId, actionId),
            ) || "";
            return `
              <div class="interface-output-action-control">
                <button class="primary-button interface-output-execute" type="button" data-output-id="${escapeHtml(outputId)}" data-action-id="${escapeHtml(actionId)}" ${starting || running ? "disabled" : ""}>${escapeHtml(starting ? "Starting…" : running ? "Running…" : item.label || "Run")}</button>
                ${item.accepts_arguments ? `
                  <details class="interface-output-action-argument-details">
                    <summary>Optional arguments</summary>
                    <label>
                      <span>One argument per non-empty line</span>
                      <textarea class="interface-output-action-arguments" rows="2" maxlength="32768" data-output-id="${escapeHtml(outputId)}" data-action-id="${escapeHtml(actionId)}">${escapeHtml(argumentDraft)}</textarea>
                    </label>
                  </details>
                ` : ""}
              </div>
            `;
          }).join("")}
          ${viewEligible ? `<button class="ghost-button interface-output-view" type="button" data-output-id="${escapeHtml(outputId)}" ${viewPending ? "disabled" : ""}>${viewPending ? "Opening…" : "View result"}</button>` : ""}
          ${keepEligible ? keptWorkspaceId
            ? `<span class="interface-output-kept">Workspace created: ${escapeHtml(output.kept_workspace_title || "Generated output")}.</span><button class="primary-button interface-output-open" type="button" data-workspace-id="${escapeHtml(keptWorkspaceId)}">Open Workspace</button><button class="ghost-button interface-output-curate" type="button" data-workspace-id="${escapeHtml(keptWorkspaceId)}">Publish</button>`
            : `<button class="ghost-button interface-output-keep" type="button" data-output-id="${escapeHtml(outputId)}" ${pending ? "disabled" : ""}>${pending ? "Saving..." : "Save as Workspace"}</button>` : ""}
        </div>
        ${keptWorkspaceId ? `<p class="interface-output-guidance">Publish can configure and check this same Workspace for Catalog; it does not make another copy.</p>` : ""}
      ` : ""}
    </article>
  `;
}

function persistActiveInterfaceLaunch(launch) {
  const launchId = String(launch && launch.launch_id || "");
  if (!launchId) {
    state.storedInterfaceLaunch = {};
    storeSessionValue(STORAGE_KEYS.activeInterfaceLaunch, null);
    return;
  }
  const coordinate = {
    launch_id: launchId,
    key: String(launch.key || ""),
    kind: String(launch.kind || ""),
    uid: String(launch.uid || ""),
    label: String(launch.label || ""),
    profile_id: String(launch.profile_id || ""),
    port: Number(launch.port || 0) || 0,
    launch_scope: String(launch.launch_scope || ""),
    source_workspace_id: String(launch.source_workspace_id || ""),
  };
  state.storedInterfaceLaunch = coordinate;
  storeSessionValue(STORAGE_KEYS.activeInterfaceLaunch, JSON.stringify(coordinate));
}

function mergeInterfaceLaunchPayload(current, incoming, launchKey) {
  const previous = current || {};
  const next = incoming || {};
  const previousResult = previous.result && typeof previous.result === "object" ? previous.result : {};
  const nextResult = next.result && typeof next.result === "object" ? next.result : null;
  let result = nextResult ? { ...previousResult, ...nextResult } : previousResult;
  if (nextResult && Array.isArray(nextResult.outputs)) {
    const previousOutputs = new Map(
      (Array.isArray(previousResult.outputs) ? previousResult.outputs : [])
        .filter((output) => output && output.id)
        .map((output) => [String(output.id), output]),
    );
    result = {
      ...result,
      outputs: nextResult.outputs.map((output) => {
        const local = previousOutputs.get(String(output && output.id || "")) || {};
        const serverWorkspaceId = String(output && output.kept_workspace_id || "");
        return {
          ...output,
          keep_pending: serverWorkspaceId ? false : Boolean(local.keep_pending),
          keep_error: serverWorkspaceId ? "" : local.keep_error || "",
          keep_request_id: local.keep_request_id || "",
          keep_state: output && output.keep_state || local.keep_state || "not-started",
          kept_workspace_id: output && output.kept_workspace_id || local.kept_workspace_id || "",
          kept_workspace_title: output && output.kept_workspace_title || local.kept_workspace_title || "",
          retry_pending: Boolean(local.retry_pending),
          retry_error: local.retry_error || "",
          view_pending: Boolean(local.view_pending),
          view_error: local.view_error || "",
          execute_pending_action_id: local.execute_pending_action_id || "",
          execute_error: local.execute_error || "",
        };
      }),
    };
  }
  const merged = { ...previous, ...next, key: launchKey, result };
  persistActiveInterfaceLaunch(merged);
  return merged;
}

function updateInterfaceOutput(outputId, patch) {
  const launch = state.interfaceLaunch;
  const result = launch && launch.result && typeof launch.result === "object" ? launch.result : null;
  const outputs = result && Array.isArray(result.outputs) ? result.outputs : [];
  if (!launch || !outputs.some((output) => String(output && output.id || "") === String(outputId || ""))) return false;
  state.interfaceLaunch = {
    ...launch,
    result: {
      ...result,
      outputs: outputs.map((output) => String(output && output.id || "") === String(outputId || "")
        ? { ...output, ...patch }
        : output),
    },
  };
  updateInterfaceOutputPanel(state.interfaceLaunch);
  return true;
}

function interfaceOutputControlRoots() {
  return [els.componentDetail, els.workspaceInterfaceLaunchStatus].filter(Boolean);
}

function updateInterfaceOutputPanel(launchState, root = null) {
  syncInterfaceLaunchActivity(launchState, root);
  const signature = interfaceOutputRenderSignature(launchState);
  const outputs = launchState && launchState.result && Array.isArray(launchState.result.outputs)
    ? launchState.result.outputs
    : [];
  const recoveryState = interfaceOutputRecoveryState(launchState);
  const surfaces = (root ? [root] : interfaceOutputControlRoots())
    .filter((surface) => Boolean(surface && surface.querySelector(".interface-launch-status")));
  const structuralChange = surfaces.some((surface) => {
    const launchStatus = surface.querySelector(".interface-launch-status");
    const panel = surface.querySelector('[data-interface-drawer-panel="outputs"]');
    return Boolean(panel) !== Boolean(outputs.length)
      || String(launchStatus && launchStatus.dataset.interfaceOutputRecoveryState || "") !== recoveryState
      || Boolean(panel && panel.dataset.outputRecoveryState !== recoveryState);
  });
  if (structuralChange) {
    renderInterfaceLaunchSurface(launchState);
    return true;
  }
  let updated = false;
  surfaces.forEach((surface) => {
    const panel = surface.querySelector(".interface-output-section");
    const list = panel && panel.querySelector(".interface-output-list");
    const pickerSlot = panel && panel.querySelector(".interface-output-picker-slot");
    if (!panel || !list || panel.dataset.outputSignature === signature) return;
    const focused = interfaceOutputFocusDescriptor(surface);
    const hadOutputFailure = panel.dataset.outputFailure === "true";
    const hasOutputFailure = outputs.some(
      (output) => String(output && output.status || "").toLowerCase() === "failed",
    );
    panel.dataset.outputSignature = signature;
    panel.dataset.outputFailure = hasOutputFailure ? "true" : "false";
    const pickerSignature = interfaceOutputPickerRenderSignature(launchState);
    if (pickerSlot && pickerSlot.dataset.pickerSignature !== pickerSignature) {
      pickerSlot.dataset.pickerSignature = pickerSignature;
      pickerSlot.innerHTML = renderInterfaceOutputTreePicker(launchState);
    }
    list.innerHTML = renderInterfaceOutputList(outputs);
    const outputToggle = surface.querySelector(".interface-output-drawer-toggle");
    if (outputToggle) outputToggle.textContent = `Outputs (${outputs.length})`;
    bindInterfaceOutputControls(surface);
    restoreInterfaceOutputFocus(surface, focused);
    if (hasOutputFailure && !hadOutputFailure) {
      setInterfaceLaunchDrawer(surface, "outputs");
    }
    updated = true;
  });
  return updated;
}

function interfaceOutputFocusDescriptor(surface) {
  const active = document.activeElement;
  if (!surface || !active || !surface.contains(active)) return null;
  const control = active.closest && active.closest("[data-output-id], [data-workspace-id]");
  if (!control) return null;
  const actionClass = [
    "interface-output-view",
    "interface-output-execute",
    "interface-output-keep",
    "interface-output-retry",
    "interface-output-open",
    "interface-output-curate",
  ].find((className) => control.classList.contains(className));
  if (!actionClass) return null;
  return {
    actionClass,
    outputId: String(control.dataset.outputId || ""),
    workspaceId: String(control.dataset.workspaceId || ""),
  };
}

function restoreInterfaceOutputFocus(surface, descriptor) {
  if (!surface || !descriptor) return;
  const selector = descriptor.outputId
    ? `.${descriptor.actionClass}[data-output-id="${cssEscape(descriptor.outputId)}"]`
    : `.${descriptor.actionClass}[data-workspace-id="${cssEscape(descriptor.workspaceId)}"]`;
  const control = surface.querySelector(selector);
  if (control) control.focus();
}

function bindInterfaceOutputControls(root, callbacks = {}) {
  if (!root) return;
  const viewOutput = callbacks.view || viewInterfaceOutput;
  const keepOutput = callbacks.keep || keepInterfaceOutput;
  const retryOutput = callbacks.retry || retryInterfaceOutput;
  const executeOutput = callbacks.execute || runInterfaceOutputAction;
  const openWorkspace = callbacks.open || selectSession;
  const curateWorkspace = callbacks.curate || openWorkspaceForCuration;
  root.querySelectorAll(".interface-output-keep").forEach((button) => {
    button.addEventListener("click", () => keepOutput(button.dataset.outputId));
  });
  root.querySelectorAll(".interface-output-view").forEach((button) => {
    button.addEventListener("click", () => viewOutput(button.dataset.outputId));
  });
  root.querySelectorAll(".interface-output-retry").forEach((button) => {
    button.addEventListener("click", () => retryOutput(button.dataset.outputId));
  });
  root.querySelectorAll(".interface-output-execute").forEach((button) => {
    button.addEventListener("click", () => executeOutput(
      button.dataset.outputId,
      button.dataset.actionId,
      interfaceOutputArgumentsFromControl(button),
    ));
  });
  root.querySelectorAll(".interface-output-action-arguments").forEach((input) => {
    input.addEventListener("input", () => rememberInterfaceOutputArgumentDraft(
      input.dataset.outputId,
      input.dataset.actionId,
      input.value,
    ));
  });
  root.querySelectorAll(".interface-output-open").forEach((button) => {
    button.addEventListener("click", () => openWorkspace(button.dataset.workspaceId));
  });
  root.querySelectorAll(".interface-output-curate").forEach((button) => {
    button.addEventListener("click", () => curateWorkspace(button.dataset.workspaceId));
  });
  root.querySelectorAll(".interface-output-tree-picker").forEach((form) => {
    if (form.dataset.bound === "true") return;
    form.dataset.bound = "true";
    const refresh = form.querySelector(".interface-output-tree-refresh");
    if (refresh) refresh.addEventListener("click", () => loadInterfaceOutputTreeChoices(form));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      captureInterfaceOutputTree(form);
    });
    loadInterfaceOutputTreeChoices(form);
  });
}

async function openWorkspaceForCuration(workspaceId) {
  if (!workspaceId) return;
  await selectSession(workspaceId);
  await openRegistrationMenu();
}

function bindComponentInterfaceLaunchControls(component, root = els.componentDetail) {
  if (!root) return;
  const stopButton = root.querySelector(".component-stop-interface");
  if (stopButton) stopButton.addEventListener("click", () => stopComponentInterface(component));
  const retryButton = root.querySelector(".component-retry-interface");
  if (retryButton) retryButton.addEventListener("click", () => launchComponentInterface(component));
  bindInterfaceLaunchDisclosureControls(root);
  bindInterfaceOutputControls(root);
}

function renderExperiments() {
  if (els.planSearch && els.planSearch.value !== state.planSearch) {
    els.planSearch.value = state.planSearch;
  }
  const query = normalizeSearch(state.planSearch);
  const plans = state.plans.filter((plan) => !query || planSearchText(plan).includes(query));
  els.planList.innerHTML = plans.map(planButton).join("") || emptyInline(query ? "No Studies match." : "No Studies yet.");
  document.querySelectorAll("[data-plan-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedPlanId = button.dataset.planId;
      syncStudioRoute();
      renderExperiments();
      renderAssistant();
    });
  });
  renderPlanDetail();
}

function normalizeSearch(value) {
  return String(value || "").trim().toLowerCase();
}

function catalogSearchText(component) {
  const summary = component.entry && component.entry.summary || {};
  const interfaceProfiles = summarizedInterfaceProfiles(component.entry && component.entry.interface);
  return normalizeSearch([
    component.kind,
    component.id,
    component.path,
    component.entry && component.entry.label,
    component.entry && component.entry.id,
    component.entry && component.entry.path,
    component.entry && component.entry.package,
    component.entry && component.entry.package_id,
    component.entry && component.entry.qualified_id,
    component.entry && component.entry.catalog_key,
    summary.description,
    summary.goal,
    summary.candidate_format,
    summary.protocol,
    summary.implementation_type,
    ...interfaceProfiles.flatMap((profile) => [profile.id, profile.label, profile.presentation && profile.presentation.port]),
    ...[].concat(component.entry && component.entry.tags || []),
    ...[].concat(summary.candidate_formats || []),
    ...[].concat(summary.metrics || []),
  ].filter(Boolean).join(" "));
}

function planSearchText(plan) {
  return normalizeSearch([
    plan.title,
    plan.source,
    plan.status,
    plan.environment && plan.environment.id,
    plan.method && plan.method.id,
    plan.metric,
    plan.direction,
  ].filter(Boolean).join(" "));
}

function studyCatalogPublicationSetup(plan) {
  const refs = [plan && plan.study, plan && plan.environment, plan && plan.method]
    .map(exactCatalogEntryRef)
    .filter((reference) => (
      reference
      && typeof reference === "object"
      && reference.source_kind === "configured-filesystem-import"
      && reference.source_id
    ));
  const sourceIds = [...new Set(refs.map((reference) => String(reference.source_id)))];
  if (!sourceIds.length) return { reason: "", sources: [] };
  const sources = sourceIds.map((sourceId) => (
    (state.catalog.sources || []).find((source) => source.source_id === sourceId)
    || {
      source_id: sourceId,
      label: "selected local package",
      actions: {
        open_workspace: {
          eligible: false,
          reason: "Refresh Catalog before opening this package setup.",
        },
      },
    }
  ));
  const labels = [...new Set(sources.map((source) => String(source.label || source.package_id || "local package")))];
  const subject = labels.length === 1 ? labels[0] : "the selected local packages";
  return {
    reason: `Publish ${subject} before saving or launching this Study. Study drafts use checked, immutable Catalog versions.`,
    sources,
  };
}

function studyCatalogPublicationStatus(setup) {
  if (!setup || !setup.reason) return "";
  const actions = setup.sources
    .map((source) => {
      const capability = source.actions && source.actions.open_workspace || {};
      const disabled = capability.eligible !== true;
      const reason = String(capability.reason || "Package setup is unavailable.");
      return `<button class="ghost-button study-open-package-setup" data-study-package-source="${escapeHtml(source.source_id || "")}" type="button" ${disabled ? `disabled title="${escapeHtml(reason)}"` : ""}>Open package setup</button>`;
    })
    .join("");
  return `
    <section class="study-action-status" role="status">
      <strong>Publish package first</strong>
      <p>${escapeHtml(setup.reason)}</p>
      ${actions ? `<div class="action-row">${actions}</div>` : ""}
    </section>
  `;
}

function blockUnpublishedStudyAction(plan, kind) {
  const setup = studyCatalogPublicationSetup(plan);
  if (!setup.reason) return false;
  setStudyActionError(plan, kind, "Publish package first", setup.reason);
  renderExperiments();
  return true;
}

function studyRuntimeEnvironmentRequirements(plan) {
  const validation = plan && plan.draft && plan.draft.validation
    || plan && plan.validation
    || plan && plan.study && plan.study.validation;
  const retainedRequirements = validation
    && validation.runtime_environment
    && Array.isArray(validation.runtime_environment.requirements)
    ? validation.runtime_environment.requirements
    : [];
  if (retainedRequirements.length) {
    return retainedRequirements.map((item) => ({
      name: String(item.name || ""),
      component: "Method",
      path: "runtime.envFromHost",
      configured: item.configured,
      source: item.source || "unknown",
    })).filter((item) => item.name);
  }
  const requirements = [];
  const add = (component) => {
    const raw = component && component.raw_config && typeof component.raw_config === "object"
      ? component.raw_config
      : {};
    componentEnvRequirements(raw)
      .filter((item) => item.path === "runtime.envFromHost")
      .forEach((item) => requirements.push({
        ...item,
        component: "Method",
      }));
  };
  add(plan && plan.method);
  const seen = new Set();
  return requirements.filter((item) => {
    const key = `${item.component}:${item.path}:${item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function studyRuntimeRequirementConfigured(requirement) {
  if (requirement && requirement.source === "host" && requirement.configured === true) {
    return true;
  }
  return configuredEnvironmentVariableNames().has(requirement && requirement.name);
}

function missingStudyRuntimeEnvironmentRequirements(plan) {
  return studyRuntimeEnvironmentRequirements(plan)
    .filter((item) => !studyRuntimeRequirementConfigured(item));
}

function studyRuntimeSetupReason(plan) {
  const missing = missingStudyRuntimeEnvironmentRequirements(plan);
  if (!missing.length) return "";
  const names = [...new Set(missing.map((item) => item.name))];
  return `Add ${names.join(", ")} in Studio Settings before launching this Run.`;
}

function studyPersistencePresentation(plan) {
  if (plan && plan.study && (plan.study.ref || plan.study.uid)) {
    return { label: "Built-in Study", status: "saved" };
  }
  if (plan && plan.draft && plan.draft.saved_as_draft) {
    return plan.draft.dirty
      ? { label: "Unsaved changes", status: "draft" }
      : { label: "Saved", status: "saved" };
  }
  return { label: "Not saved", status: "draft" };
}

function studyLaunchPresentation(plan) {
  const available = !(plan && plan.draft && plan.draft.available === false);
  if (!available) return { label: "Unavailable", status: "unavailable" };
  if (studyCatalogPublicationSetup(plan).reason) {
    return { label: "Publish first", status: "setup" };
  }
  const validation = plan && plan.draft && plan.draft.validation
    || plan && plan.validation
    || plan && plan.study && plan.study.validation;
  if (validation && validation.valid === false) {
    return { label: "Needs review", status: "review" };
  }
  if (studyRuntimeSetupReason(plan)) {
    return { label: "Setup needed", status: "setup" };
  }
  const launch = studyLaunchCapability(plan);
  if (launch && launch.eligible !== true) {
    return { label: "Run unavailable", status: "review" };
  }
  if (!plan || !plan.environment || !plan.method) {
    return { label: "Setup needed", status: "setup" };
  }
  return { label: "Ready to launch", status: "ready" };
}

function labeledStatusPill(label, status) {
  return `<span class="status-pill ${statusClass(status)}">${escapeHtml(label)}</span>`;
}

function studyStatusPills(plan) {
  const persistence = studyPersistencePresentation(plan);
  const launch = studyLaunchPresentation(plan);
  return `
    <span class="study-status-facts">
      ${labeledStatusPill(persistence.label, persistence.status)}
      ${labeledStatusPill(launch.label, launch.status)}
    </span>
  `;
}

function setStudyActionError(plan, kind, title, message) {
  if (!plan) return;
  plan.actionError = {
    kind,
    title,
    message: publicStudyMessage(message),
  };
}

function renderStudyActionStatus(plan) {
  if (!plan) return "";
  if (plan.savePending || plan.launchPending) {
    const saving = Boolean(plan.savePending);
    return `
      <section class="study-action-status" role="status" aria-live="polite">
        <strong>${saving ? "Saving changes…" : "Preparing Run…"}</strong>
        <p>${saving
          ? "Saving this Study draft."
          : "Stay on this Study while Studio checks the current configuration."}</p>
      </section>
    `;
  }
  const error = plan.actionError;
  if (!error || !error.message) return "";
  return `
    <section class="study-action-status study-action-failed" role="alert">
      <strong>${escapeHtml(error.title || "Study action failed")}</strong>
      <p>${escapeHtml(error.message)}</p>
      <small>Correct the Study if needed, then use the action above to try again.</small>
    </section>
  `;
}

function renderPlanDetail() {
  const plan = currentPlan();
  if (!plan) {
    els.planDetail.innerHTML = emptyState("Select a Study, or create one from Catalog items.");
    return;
  }
  const draftValid = Boolean(hasCurrentWorkspaceStudyDraft(plan.draft) && (!plan.draft.validation || plan.draft.validation.valid));
  const savedConfig = Boolean(plan.study && (plan.study.ref || plan.study.uid));
  const savedDraft = Boolean(plan.draft && plan.draft.saved_as_draft);
  const draftUnavailable = Boolean(savedDraft && plan.draft.available === false);
  const launchCapability = studyLaunchCapability(plan);
  const activeLaunch = studyLaunchForPlan(plan);
  const launchPreparing = Boolean(activeLaunch && !studyLaunchIsTerminal(activeLaunch));
  const trackedLaunch = state.studyLaunch;
  const otherLaunchPreparing = Boolean(
    trackedLaunch
    && trackedLaunch.planId !== plan.id
    && !studyLaunchIsTerminal(trackedLaunch)
  );
  const actionPending = Boolean(plan.savePending || plan.launchPending);
  const bindingReason = studyLaunchBindingReason(plan);
  const publicationSetup = studyCatalogPublicationSetup(plan);
  const publicationReason = publicationSetup.reason;
  const runtimeSetupReason = studyRuntimeSetupReason(plan);
  const launchEnabled = Boolean(savedConfig || plan.environment && plan.method)
    && !draftUnavailable
    && !actionPending
    && !launchPreparing
    && !otherLaunchPreparing
    && !publicationReason
    && !runtimeSetupReason
    && (!launchCapability || launchCapability.eligible === true);
  const launchReason = otherLaunchPreparing
    ? `Another Study (${trackedLaunch.planTitle || "untitled Study"}) is already being prepared. Wait for that request to finish before launching this Study.`
    : draftUnavailable
    ? publicStudyMessage(plan.draft.unavailableReason || "This saved draft could not be reopened safely.")
    : bindingReason
    ? bindingReason
    : publicationReason
    ? publicationReason
    : runtimeSetupReason
    ? runtimeSetupReason
    : launchCapability && launchCapability.eligible !== true
    ? publicStudyLaunchReason(launchCapability)
    : "";
  const launchRetryable = Boolean(
    activeLaunch && activeLaunch.failure
    || plan.actionError && plan.actionError.kind === "launch",
  );
  const launchLabel = plan.launchPending || launchPreparing
    ? "Preparing…"
    : launchRetryable
    ? "Launch again"
    : "Launch Run";
  const defaultSaveLabel = savedDraft ? "Update draft" : "Save draft";
  const saveLabel = plan.savePending
    ? "Saving…"
    : plan.actionError && plan.actionError.kind === "save"
    ? "Try saving again"
    : defaultSaveLabel;
  const locked = draftUnavailable || !plan.environment || !plan.method;
  const saveDisabled = locked || actionPending || launchPreparing || Boolean(publicationReason);
  const saveReason = draftUnavailable
    ? publicStudyMessage(plan.draft.unavailableReason || "This saved draft could not be reopened safely.")
    : bindingReason || publicationReason;
  els.planDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(plan.title)}</h2>
        <p class="path-text">${escapeHtml(plan.source)}</p>
        ${studySourceNote(plan)}
      </div>
      ${studyStatusPills(plan)}
    </div>
    <div class="study-actions">
      <div class="action-row study-action-row">
        <button class="ghost-button plan-draft" type="button" ${saveDisabled ? "disabled" : ""} ${saveReason ? `title="${escapeHtml(saveReason)}"` : ""}>${escapeHtml(saveLabel)}</button>
        <button class="primary-button plan-launch" type="button" ${launchEnabled ? "" : "disabled"} ${launchReason ? `title="${escapeHtml(launchReason)}"` : ""}>${escapeHtml(launchLabel)}</button>
        ${savedDraft ? `<details class="study-more"><summary>More</summary><button class="danger-button plan-discard-draft" type="button">Discard draft</button></details>` : ""}
      </div>
      ${studyCatalogPublicationStatus(publicationSetup)}
      ${launchReason && !publicationReason ? `<p class="study-action-reason" role="status"><strong>${runtimeSetupReason ? "Run needs setup:" : "Launch unavailable:"}</strong> ${escapeHtml(launchReason)}</p>` : ""}
      ${renderStudyActionStatus(plan)}
    </div>
    ${renderStudyLaunchStatus(plan)}
    <div class="plan-layout">
      <section class="study-config-grid">
        ${studyGuidePanel(plan)}
        ${studyConfigEditor(plan, locked)}
        ${studyReadinessPanel(plan)}
        ${studyValidationPanel(plan)}
      </section>
    </div>
  `;
  const saveButton = els.planDetail.querySelector(".plan-draft");
  if (saveButton) saveButton.addEventListener("click", () => generatePlanDraft(plan));
  els.planDetail.querySelector(".plan-launch").addEventListener("click", () => launchPlan(plan));
  const settingsButton = els.planDetail.querySelector(".study-open-environment-settings");
  if (settingsButton) settingsButton.addEventListener("click", () => openSettings({ tab: "environment" }));
  els.planDetail.querySelectorAll("[data-study-package-source]").forEach((button) => {
    button.addEventListener("click", () => openConfiguredCatalogSourceWorkspace(button.dataset.studyPackageSource));
  });
  const discardButton = els.planDetail.querySelector(".plan-discard-draft");
  if (discardButton) discardButton.addEventListener("click", () => discardStudyDraft(plan));
  const stopLaunchButton = els.planDetail.querySelector(".study-launch-stop");
  if (stopLaunchButton) stopLaunchButton.addEventListener("click", stopActiveStudyLaunch);
  if (!locked) bindPlanConfigControls(plan);
}

function studyLaunchForPlan(plan) {
  const active = state.studyLaunch;
  return active && plan && active.planId === plan.id ? active : null;
}

function studyLaunchIsTerminal(active) {
  const status = String(active && active.launch && active.launch.status || active && active.status || "");
  return ["failed", "cancelled", "rejected"].includes(status) || Boolean(active && active.failure);
}

function studyLaunchElapsedSeconds(active) {
  const serverElapsed = Number(
    (active && active.launch && active.launch.elapsed_seconds) ?? (active && active.elapsedSeconds)
  );
  const startedAt = Number(active && active.startedAt);
  const localElapsed = Number.isFinite(startedAt) && startedAt > 0
    ? Math.max(0, (Date.now() - startedAt) / 1000)
    : Number.NaN;
  if (Number.isFinite(serverElapsed) && Number.isFinite(localElapsed)) {
    return Math.max(0, serverElapsed, localElapsed);
  }
  if (Number.isFinite(serverElapsed)) return Math.max(0, serverElapsed);
  if (Number.isFinite(localElapsed)) return localElapsed;
  return 0;
}

function studyLaunchElapsedText(active) {
  return `${formatDuration(studyLaunchElapsedSeconds(active) * 1000) || "<1s"} elapsed`;
}

function refreshActiveStudyLaunchElapsed() {
  const active = state.studyLaunch;
  if (!active || studyLaunchIsTerminal(active) || !els.planDetail) return;
  const elapsed = els.planDetail.querySelector(".study-launch-elapsed");
  if (elapsed) elapsed.textContent = studyLaunchElapsedText(active);
}

function renderStudyLaunchStatus(plan) {
  const active = studyLaunchForPlan(plan);
  if (!active) return "";
  const launch = active.launch || {};
  const failure = active.failure || launch.failure;
  const stage = failure
    ? "Preparation failed"
    : launch.stage || active.stage || "Preparing Run";
  const elapsedText = studyLaunchElapsedText(active);
  const message = failure
    ? String(failure.message || active.error || "OptPilot could not prepare this Run.")
    : launch.run_id
    ? "The Run is ready. Opening it now…"
    : active.message || "OptPilot is checking this Study and preparing exact Run inputs. You can leave this page while it prepares.";
  const logs = Array.isArray(launch.log_summary) ? launch.log_summary : [];
  const status = failure
    ? "failed"
    : launch.status || (active.status === "uncertain" ? "preparing" : active.status || "preparing");
  return `
    <section class="study-launch-status ${failure ? "study-launch-failed" : ""}" ${failure ? 'role="alert"' : 'role="status"'}>
      <div>
        <h3>${escapeHtml(stage)}</h3>
        <p>${escapeHtml(message)}</p>
        ${active.stopError ? `<p class="error-text" role="alert">${escapeHtml(active.stopError)}</p>` : ""}
        <span class="study-launch-elapsed">${escapeHtml(elapsedText)}</span>
        ${logs.length ? `<details class="study-launch-log-summary"><summary>Log summary</summary><div>${logs.map((item) => `<span>${escapeHtml(item.stream || "log")} · ${escapeHtml(item.line_count ?? 0)} lines${item.truncated ? " · truncated" : ""}</span>`).join("")}</div></details>` : ""}
      </div>
      ${launch.can_stop && !active.stopPending ? `<button class="ghost-button study-launch-stop" type="button">Stop</button>` : active.stopPending ? `<button class="ghost-button" type="button" disabled>Stopping…</button>` : statusPill(status)}
    </section>
  `;
}

function studyConfigEditor(plan, locked) {
  return `
    <section class="study-card study-config-card study-primary-settings" aria-labelledby="study-primary-settings-heading">
      <header class="study-card-heading study-primary-heading">
        <div>
          <h3 id="study-primary-settings-heading">Study setup</h3>
          <p class="study-card-help">Choose what evaluates Candidates, what proposes them, the goal, and how many trials to run.</p>
        </div>
        <span class="study-card-meta">Required</span>
      </header>
      <div class="study-primary-body">
        <section class="study-settings-group" aria-labelledby="study-components-heading">
          <div class="study-settings-group-heading">
            <span class="study-step-number" aria-hidden="true">1</span>
            <div>
              <h4 id="study-components-heading">Environment and Method</h4>
              <p>The Environment evaluates Candidates. The Method proposes them.</p>
            </div>
          </div>
          <div class="control-grid">
            ${catalogSelectField("Environment", "environmentUid", plan.environment && plan.environment.uid || "", catalogChoicesForPlan("environment", plan), locked || !(state.catalog.environments || []).length, "Defines the candidate format, evaluator, available metrics, and outputs.")}
            ${catalogSelectField("Method", "methodUid", plan.method && plan.method.uid || "", catalogChoicesForPlan("method", plan), locked || !(state.catalog.methods || []).length, "Must support the Environment's Candidate format and required context.")}
          </div>
          ${studyComponentCompatibilityMessage(plan)}
        </section>
        <section class="study-settings-group" aria-labelledby="study-objective-heading">
          <div class="study-settings-group-heading">
            <span class="study-step-number" aria-hidden="true">2</span>
            <div>
              <h4 id="study-objective-heading">Goal</h4>
              <p>Choose the result that determines which Candidate is best.</p>
            </div>
          </div>
          <div class="control-grid">
            ${selectField("Metric", "metric", plan.metric || "", metricOptions(plan), locked, "Primary metric used to identify the best Candidate.")}
            ${selectField("Direction", "direction", plan.direction || "maximize", ["minimize", "maximize"], locked, "Whether lower or higher values are better.")}
          </div>
        </section>
        <section class="study-settings-group" aria-labelledby="study-budget-heading">
          <div class="study-settings-group-heading">
            <span class="study-step-number" aria-hidden="true">3</span>
            <div>
              <h4 id="study-budget-heading">Budget</h4>
              <p>Set the maximum number of Candidate evaluations for this Run.</p>
            </div>
          </div>
          <div class="control-grid">
            ${inputField("Max trials", "maxTrials", plan.maxTrials || "", "number", locked, "1", "Total Candidate evaluations to run.")}
          </div>
        </section>
      </div>
    </section>
    ${studyConfigSection("Advanced settings", "Optional", "Tune evaluation, execution, saved evidence, reproducibility, and display details only when needed.", `
      ${studyAdvancedGroup("Evaluation and reporting", "Choose how repeated results are combined and which extra metrics appear in the Run.", `
        <div class="control-grid">
          ${selectField("Aggregation", "aggregation", plan.aggregation || "mean", ["mean", "median", "min", "max", "sum", "last", "weighted_mean"], locked, "How repeated observations are reduced to one comparison value.")}
          ${inputField("Secondary metrics", "secondaryMetrics", (plan.secondaryMetrics || []).join(", "), "text", locked, "", "Additional metrics to display and record with the Run.")}
        </div>
      `)}
      ${studyAdvancedGroup("Execution limits", "Adjust concurrency and stopping behavior for unusual or expensive evaluations.", `
        <div class="control-grid">
          ${inputField("Timeout seconds", "timeoutSeconds", plan.timeoutSeconds || "", "number", locked, "1", "Per-trial time limit.")}
          ${inputField("Method callback timeout", "methodRequestTimeoutSeconds", plan.methodRequestTimeoutSeconds ?? 10, "number", locked, "1", "Maximum time for one Method proposal or observation callback. Long external model calls may need a larger value.")}
          ${inputField("Parallelism", "parallelism", plan.parallelism || "", "number", locked, "1", "Number of trials allowed to run at the same time.")}
          ${inputField("Max failures", "maxFailures", plan.maxFailures ?? "", "number", locked, "1", "Optional stop limit for failed trials. Leave blank for no limit.")}
          ${inputField("Max retries", "maxRetries", plan.maxRetries ?? "", "number", locked, "0", "Retries allowed for a failed trial.")}
          ${inputField("Max wall-clock seconds", "maxWallClockSeconds", plan.maxWallClockSeconds ?? "", "number", locked, "1", "Optional total Run time limit. Leave blank for no limit.")}
        </div>
      `)}
      ${studyAdvancedGroup("Evidence and reproducibility", "Control Run detail and the stable seed used by components that support it.", `
        <div class="control-grid">
          ${selectField("Evidence level", "evidenceLevel", plan.evidenceLevel || "standard", ["minimal", "standard", "full"], locked, "How detailed the Run records should be.")}
          ${inputField("Seed", "seed", plan.seed ?? "", "number", locked, "0", "Seed used when components support reproducibility.")}
        </div>
      `)}
      ${studyAdvancedGroup("Study details", "Optionally change how this Study is named and described.", `
        <div class="control-grid">
          ${inputField("Name", "name", plan.name || planName(plan), "text", locked, "", "Shown in the Studies and Runs lists.")}
          ${inputField("Tags", "tags", (plan.tags || []).join(", "), "text", locked, "", "Comma-separated labels for search and grouping.")}
        </div>
        ${textareaField("Description", "description", plan.description || "", locked, "Explain what this Study is trying to test.")}
      `)}
    `)}
  `;
}

function studyAdvancedGroup(title, description, body) {
  return `
    <section class="study-advanced-group">
      <header>
        <h4>${escapeHtml(title)}</h4>
        <p>${escapeHtml(description)}</p>
      </header>
      ${body}
    </section>
  `;
}

function studyConfigSection(title, meta, description, body) {
  return `
    <details class="study-card study-config-card">
      <summary>${studyCardHeading(title, meta, description)}</summary>
      <div class="study-card-body">${body}</div>
    </details>
  `;
}

function studyGuidePanel(plan) {
  const environmentLabel = plan.environment && (plan.environment.label || plan.environment.id) || "Choose an environment";
  const methodLabel = plan.method && (plan.method.label || plan.method.id) || "Choose a method";
  const metric = plan.metric || firstMetric(plan.environment || {}) || "metric";
  const direction = plan.direction || "maximize";
  const trials = Number(plan.maxTrials || 8);
  return `
    <section class="study-guide-panel">
      <div>
        <span>How this Study works</span>
        <strong>${escapeHtml(environmentLabel)} + ${escapeHtml(methodLabel)}</strong>
        <p>The Environment evaluates Candidates, and the Method proposes them. Launching starts a Run that records progress and results.</p>
      </div>
      <div class="study-guide-metric">
        <span>Goal and budget</span>
        <strong>${escapeHtml(metric)} ${escapeHtml(direction)} · ${escapeHtml(trials)} ${trials === 1 ? "trial" : "trials"}</strong>
      </div>
    </section>
  `;
}

function studyCardHeading(title, meta, description = "") {
  return `
    <div class="study-card-heading">
      <span class="study-card-chevron" aria-hidden="true">›</span>
      <div>
        <h3>${escapeHtml(title)}</h3>
        ${description ? `<p class="study-card-help">${escapeHtml(description)}</p>` : ""}
      </div>
      <span class="study-card-meta">${escapeHtml(meta)}</span>
    </div>
  `;
}

function studySourceNote(plan) {
  if (plan.study && (plan.study.ref || plan.study.uid)) {
    return `<p class="source-note">Built-in Study. Changes stay in this form until you save a draft or launch a Run.</p>`;
  }
  if (hasWorkspaceStudyDraft(plan.draft)) {
    if (plan.draft.available === false) {
      return `<p class="source-note source-note-error">${escapeHtml(publicStudyMessage(plan.draft.unavailableReason || "This saved draft could not be reopened safely. It remains listed so you can review the problem."))}</p>`;
    }
    const editState = plan.draft.dirty ? " You have unsaved changes." : "";
    const stateLabel = plan.draft.saved_as_draft ? "Saved draft" : "Prepared for launch";
    return `<p class="source-note">${escapeHtml(stateLabel)}.${escapeHtml(editState)}</p>`;
  }
  return "";
}

function hasWorkspaceStudyDraft(draft) {
  return Boolean(draft && draft.workspace_id && draft.study_relative_path);
}

function hasCurrentWorkspaceStudyDraft(draft) {
  return hasWorkspaceStudyDraft(draft) && !draft.dirty;
}

function readonlyField(label, value) {
  return `<div class="readonly-field"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "-")}</strong></div>`;
}

function inputField(label, field, value, type = "text", disabled = false, min = "", help = "") {
  return `
    <label class="control-field">
      ${controlLabelHtml(label, help)}
      <input data-plan-field="${escapeHtml(field)}" type="${escapeHtml(type)}" value="${escapeHtml(value ?? "")}" ${min !== "" ? `min="${escapeHtml(min)}"` : ""} ${disabled ? "disabled" : ""} />
    </label>
  `;
}

function textareaField(label, field, value, disabled = false, help = "") {
  return `
    <label class="control-field control-field-wide">
      ${controlLabelHtml(label, help)}
      <textarea data-plan-field="${escapeHtml(field)}" ${disabled ? "disabled" : ""}>${escapeHtml(value ?? "")}</textarea>
    </label>
  `;
}

function selectField(label, field, value, options, disabled = false, help = "") {
  const optionValues = Array.from(new Set([value, ...(options || [])].filter((item) => item !== "" && item !== null && item !== undefined)));
  return `
    <label class="control-field">
      ${controlLabelHtml(label, help)}
      <select data-plan-field="${escapeHtml(field)}" ${disabled ? "disabled" : ""}>
        ${optionValues.map((option) => `<option value="${escapeHtml(option)}" ${String(option) === String(value) ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
      </select>
    </label>
  `;
}

function catalogSelectField(label, field, value, entries, disabled = false, help = "") {
  return `
    <label class="control-field">
      ${controlLabelHtml(label, help)}
      <select data-plan-field="${escapeHtml(field)}" ${disabled ? "disabled" : ""}>
        ${(entries || []).map((entry) => {
          const label = entry.label || entry.id || entry.qualified_id || "Catalog item";
          const suffix = entry.choiceCompatibility === false ? " · incompatible" : "";
          return `<option value="${escapeHtml(entry.uid || "")}" ${entry.uid === value ? "selected" : ""}>${escapeHtml(`${label}${suffix}`)}</option>`;
        }).join("")}
      </select>
    </label>
  `;
}

function catalogChoices(selected, entries) {
  const available = entries || [];
  if (!selected || !selected.uid || available.some((entry) => entry.uid === selected.uid)) return available;
  return [selected, ...available];
}

function selectedCompatibilityPair(plan) {
  if (!plan || !plan.environment || !plan.method) return null;
  return (state.compatibility.pairs || []).find((pair) => (
    pair.environment.uid === plan.environment.uid
    && pair.method.uid === plan.method.uid
  )) || null;
}

function catalogChoicesForPlan(kind, plan) {
  const selected = kind === "environment" ? plan.environment : plan.method;
  const entries = kind === "environment" ? state.catalog.environments : state.catalog.methods;
  const counterpart = kind === "environment" ? plan.method : plan.environment;
  return catalogChoices(selected, entries).map((entry) => {
    if (!counterpart) return { ...entry, choiceCompatibility: null };
    const pair = (state.compatibility.pairs || []).find((candidate) => (
      kind === "environment"
        ? candidate.environment.uid === entry.uid && candidate.method.uid === counterpart.uid
        : candidate.method.uid === entry.uid && candidate.environment.uid === counterpart.uid
    ));
    return { ...entry, choiceCompatibility: pair ? pair.compatible === true : null };
  }).sort((left, right) => {
    const rank = (entry) => entry.choiceCompatibility === true ? 0 : entry.choiceCompatibility === null ? 1 : 2;
    return rank(left) - rank(right)
      || String(left.label || left.id || "").localeCompare(String(right.label || right.id || ""));
  });
}

function studyComponentCompatibilityMessage(plan) {
  const pair = selectedCompatibilityPair(plan);
  if (!pair || pair.compatible === true) return "";
  const reasons = (pair.checks || []).filter((check) => check.ok !== true).map((check) => check.message)
    .concat(pair.reasons || [])
    .filter(Boolean);
  const message = reasons[0] || "This Environment and Method do not use the same Candidate format.";
  return `<p class="study-action-reason" role="status"><strong>Incompatible selection:</strong> ${escapeHtml(message)}</p>`;
}

function controlLabelHtml(label, help = "") {
  return `
    <span class="control-label">
      <strong>${escapeHtml(label)}</strong>
      ${help ? `<small>${escapeHtml(help)}</small>` : ""}
    </span>
  `;
}

function metricOptions(plan) {
  const metrics = plan.environment && plan.environment.summary && plan.environment.summary.metrics || [];
  return metrics.length ? metrics : [plan.metric || "score"];
}

function bindPlanConfigControls(plan) {
  els.planDetail.querySelectorAll("[data-plan-field]").forEach((control) => {
    const eventName = control.tagName === "SELECT" ? "change" : "input";
    control.addEventListener(eventName, () => {
      updatePlanField(plan, control.dataset.planField, control.value);
      if (control.dataset.planField === "environmentUid" || control.dataset.planField === "methodUid") {
        renderExperiments();
      } else {
        refreshPlanPreview(plan);
      }
    });
  });
}

function updatePlanField(plan, field, value) {
  if (field === "methodRequestTimeoutSeconds") {
    plan.methodRequestTimeoutSeconds = value;
    plan.actionError = null;
    return;
  }
  if (plan.study) convertSavedPlanToDraft(plan);
  plan.actionError = null;
  plan.draftSaveRequestId = null;
  plan.launchPreparationRequestId = null;
  plan.draftActionId = null;
  const previousDraft = plan.draft;
  if (field === "environmentUid") {
    const entry = catalogEntryByUid("environment", value);
    if (entry) {
      plan.environment = entry;
      if (!metricOptions(plan).includes(plan.metric)) {
        plan.metric = firstMetric(entry) || plan.metric || "";
      }
    }
  } else if (field === "methodUid") {
    const entry = catalogEntryByUid("method", value);
    if (entry) plan.method = entry;
  } else if (field === "secondaryMetrics") {
    plan.secondaryMetrics = value.split(",").map((item) => item.trim()).filter(Boolean);
  } else if (field === "tags") {
    plan.tags = value.split(",").map((item) => item.trim()).filter(Boolean);
  } else {
    plan[field] = value;
  }
  const compatibilityPair = selectedCompatibilityPair(plan);
  plan.checks = compatibilityPair
    ? compatibilityChecks(compatibilityPair)
    : [];
  plan.draft = hasWorkspaceStudyDraft(previousDraft)
    ? { ...previousDraft, dirty: true, error: "", validation: null }
    : null;
  plan.validation = null;
  plan.yaml = planYamlPreview(plan);
  plan.status = "draft";
}

function convertSavedPlanToDraft(plan) {
  plan.originalStudy = plan.study;
  plan.study = null;
  plan.source = "draft copy";
  plan.status = "draft";
  plan.draft = null;
  plan.validation = null;
  plan.yaml = planYamlPreview(plan);
}

function refreshPlanPreview(plan) {
  const validation = els.planDetail.querySelector(".validation-box");
  if (validation && !plan.draft) validation.innerHTML = "";
  const validationPanel = els.planDetail.querySelector(".study-validation-section");
  if (validationPanel && !plan.draft) validationPanel.remove();
  const launchButton = els.planDetail.querySelector(".plan-launch");
  if (launchButton) launchButton.textContent = "Launch Run";
  const saveButton = els.planDetail.querySelector(".plan-draft");
  if (saveButton) saveButton.textContent = plan.draft && plan.draft.saved_as_draft ? "Update draft" : "Save draft";
}

async function discardStudyDraft(plan) {
  const draft = plan && plan.draft;
  if (!draft || !draft.saved_as_draft || !draft.draft_id || !draft.draft_revision) return;
  if (!window.confirm("Discard this saved Study draft? Existing Runs are not affected.")) return;
  const result = await postJson(
    `/api/studies/drafts/${encodeURIComponent(draft.draft_id)}/discard`,
    {
      request_id: newRequestId(),
      expected_revision: draft.draft_revision,
    },
    { tolerateError: true },
  );
  if (result && result.error) {
    plan.validation = { valid: false, errors: [result.error] };
    renderExperiments();
    return;
  }
  await loadStudyDrafts();
  const discardedId = plan.id;
  state.plans = buildPlans();
  if (state.selectedPlanId === discardedId) {
    state.selectedPlanId = state.plans[0] && state.plans[0].id || null;
  }
  syncStudioRoute();
  renderExperiments();
}

function runProjectionNotice(unavailable) {
  if (!unavailable || Number(unavailable.count || 0) <= 0) return "";
  const limited = Number(unavailable.limited_count || 0);
  const hidden = Number(unavailable.hidden_count || 0);
  const messages = [];
  if (limited > 0) {
    messages.push(`${limited} older ${limited === 1 ? "Run is" : "Runs are"} shown with limited Study details after this OptPilot upgrade`);
  }
  if (hidden > 0) {
    messages.push(`${hidden} ${hidden === 1 ? "Run could" : "Runs could"} not be shown because saved data could not be verified`);
  }
  return `
    <div class="run-projection-notice" role="status">
      <strong>Some Runs could not be fully loaded.</strong>
      <span>${escapeHtml(messages.join(". "))}. Existing Run data was not changed.</span>
    </div>
  `;
}

function renderRuns() {
  document.querySelectorAll("[data-run-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.runFilter === state.runStatusFilter);
  });
  const query = els.runFilter ? els.runFilter.value.trim().toLowerCase() : "";
  const rows = state.runs;
  const runs = rows.filter((run) => {
    const matchesStatus = runMatchesStatusFilter(runStatus(run), state.runStatusFilter);
    const matchesSearch = !query || runSearchText(run).includes(query);
    return matchesStatus && matchesSearch;
  });
  if (els.totalRuns) els.totalRuns.textContent = String(rows.length);
  if (els.runningRuns) els.runningRuns.textContent = String(rows.filter((run) => runStatus(run) === "running").length);
  if (els.completedTrials) els.completedTrials.textContent = String(sum(state.runs.map((run) => runCounts(run).terminalTrials)));
  if (els.failureCount) els.failureCount.textContent = String(sum(state.runs.map((run) => runCounts(run).finalFailures)));
  const notice = runProjectionNotice(state.runUnavailable);
  const list = runs.map(runRow).join("") || emptyInline("No runs match.");
  els.runsTable.innerHTML = `${notice}${list}`;
  document.querySelectorAll(".run-row").forEach((row) => {
    row.addEventListener("click", () => loadRunDetail(row.dataset.runId));
  });
}

function runMatchesStatusFilter(status, filter) {
  if (filter === "all") return true;
  if (filter === "completed") return status === "completed" || status === "succeeded";
  if (filter === "incomplete") return !["completed", "succeeded", "failed", "cancelled"].includes(status);
  return status === filter;
}

function runSearchText(run) {
  const objective = runObjective(run);
  return `${canonicalRunId(run)} ${run.name || ""} ${runStatus(run)} ${run.stop_code || ""} ${objective.name} ${objective.direction}`.toLowerCase();
}

async function loadRunDetail(runId, options = {}) {
  if (state.selectionContentView && state.selectionContentView.run_id !== runId) {
    await closeSelectionContentView({ silent: true, render: false });
  }
  const requestSeq = ++state.runDetailRequestSeq;
  state.runPageRequestSeq += 1;
  state.runPageLoadingKind = null;
  if (state.assistantRunSelection && state.assistantRunSelection.run_id !== runId) {
    state.assistantRunSelection = null;
  }
  const sameSelectedRun = state.selectedRunId === runId;
  const preserveCandidateRoute = Boolean(
    state.routedCandidateId
    && (options.fromRoute || options.keepTab && sameSelectedRun),
  );
  state.selectedRunId = runId;
  if (!preserveCandidateRoute) {
    state.routedCandidateId = null;
    state.routedCandidateResolution = null;
    state.routedCandidateFocusApplied = "";
  }
  if (!options.fromRoute) syncStudioRoute();
  const candidateQuery = preserveCandidateRoute
    ? `?candidate_id=${encodeURIComponent(state.routedCandidateId)}`
    : "";
  const detail = await getJson(`/api/runs/${encodeURIComponent(runId)}${candidateQuery}`);
  if (!coherentRunDetail(detail, runId)) throw new Error("Run data changed during refresh. Try again.");
  if (requestSeq !== state.runDetailRequestSeq || state.selectedRunId !== runId) return;
  state.selectedRun = detail;
  state.routedCandidateResolution = preserveCandidateRoute
    ? detail.candidate_resolution || null
    : null;
  selectRunActionContext(runId);
  selectCandidateComparisonContext(runId, detail.workbench.head);
  selectOperatorJobsRun(runId);
  if (preserveCandidateRoute) state.activeRunTab = "candidate";
  else if (!options.keepTab) state.activeRunTab = "overview";
  if (!options.skipListRender) renderRuns();
  renderRunDetail();
  renderAssistant();
  loadSelectedRunOperatorJobs({ silent: state.operatorJobsLoaded });
}

function runLineageHtml(lineage, fallbackStudyName = "") {
  if (!lineage || lineage.schema !== "optpilot.run-lineage-summary.v1") return "";
  const origin = lineage.origin && typeof lineage.origin === "object" ? lineage.origin : {};
  const children = Array.isArray(lineage.re_evaluation_runs) ? lineage.re_evaluation_runs : [];
  const childOrigin = origin.kind === "exact-reevaluation" && origin.parent_run_id;
  const studyName = origin.study_name || fallbackStudyName || "this Study";
  return `
    <section class="run-origin-banner" aria-label="Run origin">
      <div>
        <strong>${childOrigin
          ? `Re-evaluation of Candidate ${escapeHtml(origin.candidate_id || "-")} from Run ${escapeHtml(origin.parent_run_id)}`
          : `Launched from Study ${escapeHtml(studyName)}`}</strong>
        <span>${childOrigin
          ? "This is a separate recorded Run using the source Candidate and the same trial settings."
          : "This Run records the Study's progress and results."}</span>
      </div>
      ${childOrigin ? `<button class="ghost-button compact-action" data-open-lineage-run="${escapeHtml(origin.parent_run_id)}" data-open-lineage-candidate="${escapeHtml(origin.candidate_id || "")}" type="button">${origin.candidate_id ? "Open source Candidate" : "Open source Run"}</button>` : ""}
      ${children.length ? `
        <details class="run-related-runs">
          <summary>${escapeHtml(children.length)} re-evaluation Run${children.length === 1 ? "" : "s"}</summary>
          <div>${children.map((child) => `<button class="ghost-button compact-action" data-open-lineage-run="${escapeHtml(child.run_id)}" type="button">${escapeHtml(child.candidate_id ? `Candidate ${child.candidate_id}` : child.run_id)}</button>`).join("")}</div>
        </details>
      ` : ""}
    </section>
  `;
}

function renderRunDetail() {
  renderSelectionContentHost();
  const detail = state.selectedRun;
  if (!detail) {
    els.runDetail.innerHTML = emptyState("Select a Run to see its progress and Candidates.");
    return;
  }
  const run = detail.run;
  if (!run || !detail.workbench) {
    els.runDetail.innerHTML = emptyState("This Run is not ready to show a consistent result view yet.");
    return;
  }
  const summary = detail.workbench.summary || run;
  const runId = canonicalRunId(run) || summary.run_id;
  const currentRunHead = detail.workbench.head || {};
  let staleCandidateTrySelectionId = "";
  if (
    state.pendingCandidateTry
    && (
      state.pendingCandidateTry.run_id !== runId
      || state.pendingCandidateTry.candidate_id !== String(state.routedCandidateId || "")
      || Number(state.pendingCandidateTry.run_head && state.pendingCandidateTry.run_head.revision) !== Number(currentRunHead.revision)
      || Number(state.pendingCandidateTry.run_head && state.pendingCandidateTry.run_head.sequence) !== Number(currentRunHead.sequence)
    )
  ) {
    const sameCandidate = state.pendingCandidateTry.run_id === runId
      && state.pendingCandidateTry.candidate_id === String(state.routedCandidateId || "");
    if (sameCandidate) {
      staleCandidateTrySelectionId = state.pendingCandidateTry.selection_id;
      state.candidateTryNotice = "Try options changed because the Run was refreshed. Review the current options before trying again.";
    }
    closeCandidateTrySheet({ restoreFocus: false });
  }
  const runLabel = run.name || runId;
  const status = runStatus(summary);
  const objective = runObjective(summary);
  const counts = runCounts(summary);
  const headlineResult = runHeadlineResult(detail);
  const budget = summary.budget || {};
  const overview = exactRunOverview(detail);
  const overviewCounts = overview && overview.counts || {};
  const candidateCounts = overviewCounts.candidates || {};
  const trialCounts = overviewCounts.logical_trials || {};
  const plannedTrials = trialCounts.planned ?? budget.max_trials;
  const progress = plannedTrials == null
    ? `${counts.terminalTrials} complete`
    : `${counts.terminalTrials} / ${plannedTrials}`;
  const completeCandidates = candidateCounts.complete
    ?? Number(overview && overview.objective_series && overview.objective_series.total_complete_candidates || 0);
  const completionMessage = runCompletionMessage(summary, status);
  const canStopRun = Boolean(run.can_stop);
  const technicalTabs = runTechnicalTabs();
  const activeTechnicalTab = technicalTabs.find(([tab]) => tab === state.activeRunTab);
  els.runDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(runLabel)}</h2>
        ${runLabel !== runId ? `<p class="run-identity" title="${escapeHtml(runId)}">Run ${escapeHtml(runId)}</p>` : ""}
      </div>
      <div class="detail-actions">
        ${statusPill(status)}
        ${canStopRun ? `<button class="ghost-button stop-selected-run" type="button">Stop Run</button>` : ""}
      </div>
    </div>
    ${runLineageHtml(detail.lineage, run.name)}
    <div class="detail-stats run-headline-stats">
      <div><span>Trial progress</span><strong>${escapeHtml(progress)}</strong></div>
      <div><span>Complete Candidates</span><strong>${escapeHtml(completeCandidates)}</strong></div>
      <div><span>Objective</span><strong>${escapeHtml(`${objective.name || "Not reported"}${objective.direction ? ` · ${objective.direction}` : ""}`)}</strong></div>
      <div><span>${escapeHtml(headlineResult.label)}</span><strong>${escapeHtml(headlineResult.value)}</strong></div>
    </div>
    ${completionMessage ? `<p class="muted-text run-stop-code">${escapeHtml(completionMessage)}</p>` : ""}
    <div class="tabs" ${runWorkbenchTabs(detail).some(([tab]) => tab === state.activeRunTab) ? 'role="tablist" aria-orientation="horizontal"' : ""} aria-label="Run result sections" data-run-tablist>
      ${runWorkbenchTabs(detail).map(([tab, label]) => runTabButtonHtml(tab, label, detail)).join("")}
    </div>
    <details class="run-more-navigation" ${activeTechnicalTab ? "open" : ""}>
      <summary>Run details${activeTechnicalTab ? ` · ${escapeHtml(activeTechnicalTab[1])}` : ""}</summary>
      <div class="run-more-actions">
        ${technicalTabs.map(([tab, label]) => `<button class="ghost-button compact-action ${state.activeRunTab === tab ? "active" : ""}" data-run-tab="${tab}" type="button" aria-pressed="${state.activeRunTab === tab ? "true" : "false"}">${escapeHtml(label)}</button>`).join("")}
      </div>
    </details>
    ${runTabPanelHtml(detail)}
  `;
  document.querySelectorAll("[data-run-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activateRunTab(button.dataset.runTab, { restoreFocus: button.getAttribute("role") === "tab" });
    });
  });
  const runTablist = els.runDetail.querySelector("[data-run-tablist]");
  if (runTablist) runTablist.addEventListener("keydown", handleRunTablistKeydown);
  els.runDetail.querySelectorAll("[data-open-lineage-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      const targetRunId = button.dataset.openLineageRun;
      const candidateId = button.dataset.openLineageCandidate || "";
      state.routedCandidateId = candidateId || null;
      await loadRunDetail(targetRunId, { fromRoute: Boolean(candidateId) });
      syncStudioRoute();
    });
  });
  const stopRunButton = els.runDetail.querySelector(".stop-selected-run");
  if (stopRunButton) {
    stopRunButton.addEventListener("click", () => openRunStopConfirmation(run, stopRunButton));
  }
  els.runDetail.querySelectorAll("[data-run-page-more]").forEach((button) => {
    button.addEventListener("click", () => loadMoreRunPage(button.dataset.runPageMore));
  });
  els.runDetail.querySelectorAll(".run-metric-select").forEach((select) => {
    select.addEventListener("change", () => {
      const metricRunId = select.dataset.runId || runId;
      state.runMetricSelections[metricRunId] = select.value;
      renderRunDetail();
    });
  });
  bindWorkbenchEntityActions();
  bindOperatorJobEvents();
  if (state.routedCandidateId) {
    window.requestAnimationFrame(ensureFocusedCandidateInspection);
  }
  if (staleCandidateTrySelectionId) {
    restoreFocusedCandidateTryFocus(staleCandidateTrySelectionId, "notice");
  }
}

function selectRunActionContext(runId) {
  const value = runId || null;
  if (state.workbenchActionRunId === value) return;
  state.workbenchActionRunId = value;
  state.pendingWorkbenchActions = new Set();
  state.workbenchActionErrors = {};
  state.environmentPreviewProfileSelections = {};
  state.semanticInspections = {};
  state.expandedWorkbenchSelections = new Set();
  state.reviewPendingSelectionIds = new Set();
  state.reviewPendingOperatorJobIds = new Set();
  state.reviewSelectionErrors = {};
  state.reviewOperatorJobErrors = {};
  state.reviewSavePending = false;
  state.reviewDeletePending = false;
  state.reviewError = "";
  state.candidateTryNotice = "";
}

function selectCandidateComparisonContext(runId, head) {
  const normalizedRunId = runId || null;
  const normalizedHead = runHead({ head });
  if (
    state.candidateComparisonRunId === normalizedRunId
    && sameRunHead(state.candidateComparisonHead, normalizedHead)
  ) return;
  state.candidateComparisonRequestSeq += 1;
  state.candidateComparisonRunId = normalizedRunId;
  state.candidateComparisonHead = normalizedHead;
  state.candidateComparisonBaseline = null;
  state.candidateComparisonCandidate = null;
  state.candidateComparisonProjection = null;
  state.candidateComparisonLoading = false;
  state.candidateComparisonError = "";
}

function selectOperatorJobsRun(runId) {
  const value = runId || null;
  if (state.operatorJobsRunId === value) return;
  state.operatorJobsRequestSeq += 1;
  state.operatorJobDetailRequestSeq += 1;
  state.operatorJobsRunId = value;
  state.operatorJobs = [];
  state.operatorJobsLoaded = false;
  state.operatorJobsLoading = Boolean(value);
  state.operatorJobsRefreshInFlight = false;
  state.operatorJobsError = "";
  state.selectedOperatorJobId = null;
  state.selectedOperatorJob = null;
  state.operatorJobDetailError = "";
  state.pendingOperatorJobStops = new Set();
  state.operatorJobStopErrors = {};
  state.operatorJobOutputActions = {};
}

function selectedCanonicalRunId() {
  const runId = state.selectedRunId;
  if (!runId) return "";
  return runId;
}

async function loadSelectedRunOperatorJobs(options = {}) {
  const runId = selectedCanonicalRunId();
  if (!runId) return;
  selectOperatorJobsRun(runId);
  if (state.operatorJobsRefreshInFlight) return;
  const requestSeq = ++state.operatorJobsRequestSeq;
  state.operatorJobsRefreshInFlight = true;
  if (!options.silent || !state.operatorJobsLoaded) state.operatorJobsLoading = true;
  renderOperatorJobsPanel();
  try {
    const payload = await getJson(`/api/runs/${encodeURIComponent(runId)}/operator-jobs`);
    if (requestSeq !== state.operatorJobsRequestSeq || state.operatorJobsRunId !== runId) return;
    if (payload.run_id && payload.run_id !== runId) throw new Error("Candidate tries belong to another Run.");
    state.operatorJobs = operatorJobsFromPayload(payload);
    state.operatorJobsLoaded = true;
    state.operatorJobsError = "";
    if (!state.selectedOperatorJobId || !state.operatorJobs.some((job) => job.job_id === state.selectedOperatorJobId)) {
      state.selectedOperatorJobId = state.operatorJobs[0] && state.operatorJobs[0].job_id || null;
      state.selectedOperatorJob = state.operatorJobs[0] || null;
    } else {
      const summary = state.operatorJobs.find((job) => job.job_id === state.selectedOperatorJobId);
      if (!state.selectedOperatorJob || Number(summary.revision || 0) > Number(state.selectedOperatorJob.revision || 0)) {
        state.selectedOperatorJob = summary;
      }
    }
    renderOperatorJobsPanel();
    if (state.selectedOperatorJobId) {
      await loadOperatorJobDetail(state.selectedOperatorJobId, { silent: true });
    }
  } catch (error) {
    if (requestSeq === state.operatorJobsRequestSeq && state.operatorJobsRunId === runId) {
      state.operatorJobsError = boundedPublicActionError(error, "Candidate tries are temporarily unavailable.");
    }
  } finally {
    if (requestSeq === state.operatorJobsRequestSeq) {
      state.operatorJobsRefreshInFlight = false;
      state.operatorJobsLoading = false;
      renderOperatorJobsPanel();
    }
  }
}

function operatorJobsFromPayload(payload) {
  const jobs = Array.isArray(payload) ? payload : payload && (payload.jobs || payload.operator_jobs || payload.items);
  return (Array.isArray(jobs) ? jobs : []).filter((job) => job && typeof job === "object" && job.job_id);
}

function operatorJobFromPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const job = payload.job || payload.operator_job || payload;
  return job && typeof job === "object" && job.job_id ? job : null;
}

function operatorJobRunId(job) {
  return String(job && job.target && job.target.run_id || "");
}

function upsertOperatorJob(job) {
  if (!job || !job.job_id) return;
  const runId = operatorJobRunId(job);
  if (runId && state.operatorJobsRunId && runId !== state.operatorJobsRunId) return;
  const index = state.operatorJobs.findIndex((item) => item.job_id === job.job_id);
  if (index < 0) state.operatorJobs = [job, ...state.operatorJobs];
  else state.operatorJobs = state.operatorJobs.map((item, itemIndex) => itemIndex === index ? job : item);
  state.operatorJobsLoaded = true;
}

async function loadOperatorJobDetail(jobId, options = {}) {
  const runId = state.operatorJobsRunId;
  if (!runId || !jobId) return;
  const requestSeq = ++state.operatorJobDetailRequestSeq;
  if (!options.silent) {
    state.operatorJobDetailError = "";
    renderOperatorJobsPanel();
  }
  try {
    const payload = await getJson(`/api/operator-jobs/${encodeURIComponent(jobId)}`);
    if (requestSeq !== state.operatorJobDetailRequestSeq || state.operatorJobsRunId !== runId) return;
    if (payload.run_id && payload.run_id !== runId) throw new Error("Candidate try details belong to another Run.");
    const job = operatorJobFromPayload(payload);
    if (!job || job.job_id !== jobId) throw new Error("Candidate try details are incomplete.");
    if (operatorJobRunId(job) && operatorJobRunId(job) !== runId) throw new Error("Candidate try belongs to another Run.");
    upsertOperatorJob(job);
    if (state.selectedOperatorJobId === jobId) state.selectedOperatorJob = job;
    state.operatorJobDetailError = "";
  } catch (error) {
    if (requestSeq === state.operatorJobDetailRequestSeq && state.selectedOperatorJobId === jobId) {
      state.operatorJobDetailError = boundedPublicActionError(error, "Candidate try details are temporarily unavailable.");
    }
  } finally {
    if (requestSeq === state.operatorJobDetailRequestSeq) renderOperatorJobsPanel();
  }
}

function selectOperatorJob(jobId) {
  if (!jobId) return;
  state.selectedOperatorJobId = jobId;
  state.selectedOperatorJob = state.operatorJobs.find((job) => job.job_id === jobId) || null;
  state.operatorJobDetailError = "";
  renderOperatorJobsPanel();
  loadOperatorJobDetail(jobId);
}

async function stopOperatorJob(jobId) {
  const job = state.operatorJobs.find((item) => item.job_id === jobId) || state.selectedOperatorJob;
  if (!job || !operatorJobIsActive(job) || !job.can_stop || state.pendingOperatorJobStops.has(jobId)) return;
  state.pendingOperatorJobStops.add(jobId);
  delete state.operatorJobStopErrors[jobId];
  renderOperatorJobsPanel();
  try {
    const payload = await postJson(`/api/operator-jobs/${encodeURIComponent(jobId)}/stop`, {
      schema: "optpilot.operator-job-stop-request.v1",
      request_id: newRequestId(),
    });
    const updated = operatorJobFromPayload(payload);
    if (!updated || updated.job_id !== jobId) throw new Error("The stop response for this try is incomplete.");
    upsertOperatorJob(updated);
    state.selectedOperatorJobId = jobId;
    state.selectedOperatorJob = updated;
  } catch (error) {
    state.operatorJobStopErrors[jobId] = boundedPublicActionError(error, "The stop request could not be completed.");
  } finally {
    state.pendingOperatorJobStops.delete(jobId);
    renderOperatorJobsPanel();
    loadSelectedRunOperatorJobs({ silent: true });
  }
}

function operatorJobOutputActionKey(jobId, outputId) {
  return `${String(jobId || "")}:${String(outputId || "")}`;
}

function patchOperatorJobOutputAction(jobId, outputId, patch) {
  if (!jobId || !outputId) return false;
  const key = operatorJobOutputActionKey(jobId, outputId);
  state.operatorJobOutputActions[key] = {
    ...(state.operatorJobOutputActions[key] || {}),
    ...patch,
  };
  renderOperatorJobsPanel();
  return true;
}

function operatorJobOutputsForRender(job) {
  const bundle = job && job.interface_outputs && typeof job.interface_outputs === "object"
    ? job.interface_outputs
    : {};
  const outputs = Array.isArray(bundle.outputs) ? bundle.outputs : [];
  return outputs.map((output) => ({
    ...output,
    retained: bundle.lifecycle === "retained",
    kind: output && output.kind,
    ...(state.operatorJobOutputActions[
      operatorJobOutputActionKey(job && job.job_id, output && output.id)
    ] || {}),
  }));
}

async function viewOperatorJobOutput(jobId, outputId) {
  const job = state.operatorJobs.find((item) => item.job_id === jobId) || state.selectedOperatorJob;
  const output = operatorJobOutputsForRender(job).find((item) => String(item && item.id || "") === String(outputId || ""));
  const viewAction = output && output.actions && output.actions.view_read_only;
  const runId = operatorJobRunId(job);
  if (!jobId || !outputId || !runId || !viewAction || !viewAction.eligible) return;
  if (!patchOperatorJobOutputAction(jobId, outputId, {
    view_pending: true,
    view_error: "",
  })) return;
  try {
    if (state.selectionContentView) {
      await closeSelectionContentView({ silent: true });
    }
    const payload = await postJson(
      `/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/view`,
      {
        schema: "optpilot.operator-job-output-content-view-request.v1",
        content_session_id: state.selectionContentSessionId || null,
      },
    );
    await openSelectionContentView(
      payload,
      {
        kind: "artifact",
        id: outputId,
        selection: { kind: "artifact", entity_id: outputId },
      },
      runId,
      {
        requireExactHead: false,
        displayKind: "Result",
        displayId: String(output.label || outputId),
        contextLabel: "Read-only result files saved with this Candidate try",
      },
    );
    patchOperatorJobOutputAction(jobId, outputId, {
      view_pending: false,
      view_error: "",
    });
  } catch (error) {
    patchOperatorJobOutputAction(jobId, outputId, {
      view_pending: false,
      view_error: boundedPublicActionError(error, "This saved result could not be opened."),
    });
  }
}

async function retryOperatorJobOutput(jobId, outputId) {
  if (!jobId || !outputId || !patchOperatorJobOutputAction(jobId, outputId, {
    retry_pending: true,
    retry_error: "",
  })) return;
  try {
    const payload = await postJson(
      `/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/retry`,
      {},
    );
    const updated = operatorJobFromPayload(payload);
    if (!updated || updated.job_id !== jobId) throw new Error("Output retry response is incomplete.");
    upsertOperatorJob(updated);
    if (state.selectedOperatorJobId === jobId) state.selectedOperatorJob = updated;
    patchOperatorJobOutputAction(jobId, outputId, {
      retry_pending: false,
      retry_error: "",
    });
  } catch (error) {
    patchOperatorJobOutputAction(jobId, outputId, {
      retry_pending: false,
      retry_error: boundedPublicActionError(error, "This output capture could not be retried."),
    });
  }
}

async function keepOperatorJobOutput(jobId, outputId) {
  const actionKey = operatorJobOutputActionKey(jobId, outputId);
  const requestId = state.operatorJobOutputActions[actionKey] && state.operatorJobOutputActions[actionKey].keep_request_id
    || newRequestId();
  if (!jobId || !outputId || !patchOperatorJobOutputAction(jobId, outputId, {
    keep_pending: true,
    keep_error: "",
    keep_request_id: requestId,
  })) return;
  try {
    const payload = await postJson(
      `/api/operator-jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(outputId)}/keep`,
      { request_id: requestId },
    );
    if (!payload.workspace || typeof payload.workspace !== "object") {
      throw new Error("Keep response did not include an editable workspace.");
    }
    const workspace = mergeUiWorkspace(payload.workspace);
    if (!workspace) throw new Error("Keep response did not include an editable workspace.");
    const updated = operatorJobFromPayload(payload);
    if (updated && updated.job_id === jobId) {
      upsertOperatorJob(updated);
      if (state.selectedOperatorJobId === jobId) state.selectedOperatorJob = updated;
    }
    patchOperatorJobOutputAction(jobId, outputId, {
      keep_pending: false,
      keep_error: "",
      kept_workspace_id: workspace.id,
      kept_workspace_title: workspace.title,
    });
  } catch (error) {
    patchOperatorJobOutputAction(jobId, outputId, {
      keep_pending: false,
      keep_error: boundedPublicActionError(error, "This output could not be kept as a workspace."),
    });
  }
}

function operatorJobIsActive(job) {
  return ["planned", "awaiting_approval", "queued", "starting", "running", "stopping"].includes(String(job && job.state || ""));
}

function operatorJobsSection(runId, candidateId = "") {
  return `<section class="operator-jobs-section" data-operator-jobs-panel data-run-id="${escapeHtml(runId)}" data-candidate-id="${escapeHtml(candidateId)}">${operatorJobsPanelBody(runId, candidateId)}</section>`;
}

function renderOperatorJobsPanel() {
  if (!els.runDetail) return;
  const panel = els.runDetail.querySelector("[data-operator-jobs-panel]");
  const runId = selectedCanonicalRunId();
  if (!panel || !runId || panel.dataset.runId !== runId || state.operatorJobsRunId !== runId) return;
  const focusedJob = document.activeElement
    && document.activeElement.closest
    && document.activeElement.closest("[data-operator-job-id]");
  const focusedJobId = focusedJob && focusedJob.dataset.operatorJobId || "";
  panel.innerHTML = operatorJobsPanelBody(runId, panel.dataset.candidateId || "");
  bindOperatorJobEvents();
  if (focusedJobId) {
    window.requestAnimationFrame(() => {
      const target = [...panel.querySelectorAll("[data-operator-job-id]")]
        .find((button) => button.dataset.operatorJobId === focusedJobId);
      if (target && typeof target.focus === "function") target.focus();
    });
  }
}

function operatorJobsPanelBody(runId, candidateId = "") {
  const allJobs = state.operatorJobsRunId === runId ? state.operatorJobs : [];
  const jobs = candidateId
    ? allJobs.filter((job) => String(job && job.target && job.target.candidate_id || "") === candidateId)
      .sort((left, right) => (
        Number(right && (right.updated_at || right.created_at) || 0)
        - Number(left && (left.updated_at || left.created_at) || 0)
      ))
    : allJobs;
  const selected = state.selectedOperatorJobId
    ? jobs.find((job) => job.job_id === state.selectedOperatorJobId)
      || (
        state.selectedOperatorJob
        && jobs.some((job) => job.job_id === state.selectedOperatorJob.job_id)
        ? state.selectedOperatorJob
        : null
      )
    : null;
  const visibleSelected = selected || (candidateId && jobs.length ? jobs[0] : null);
  const visibleSelectedJobId = visibleSelected && visibleSelected.job_id || "";
  const loadingOnly = state.operatorJobsLoading && !state.operatorJobsLoaded;
  return `
    <div class="operator-jobs-heading">
      <div>
        <h3>Candidate tries</h3>
        <p>${candidateId ? "Trying this Candidate" : "These tries"} ${candidateId ? "does" : "do"} not use the Run's trial budget or change its recorded results, ranking, or best Candidate.</p>
      </div>
      <span class="tag">${escapeHtml(jobs.length)} ${jobs.length === 1 ? "try" : "tries"}</span>
    </div>
    ${state.operatorJobsError ? `<div class="operator-job-notice error" role="alert"><span>Candidate tries could not be refreshed: ${escapeHtml(state.operatorJobsError)}</span><button class="ghost-button compact-action operator-jobs-retry" type="button">Retry</button></div>` : ""}
    ${loadingOnly ? `<div class="operator-job-empty">Loading Candidate tries…</div>` : jobs.length ? `
      <div class="operator-jobs-layout">
        <div class="operator-job-list" role="group" aria-label="Candidate tries">
          ${jobs.map((job) => renderOperatorJobRow(job, visibleSelectedJobId)).join("")}
        </div>
        <div class="operator-job-detail">
          ${visibleSelected ? renderOperatorJobSummary(visibleSelected) : `<div class="operator-job-empty">Select a try to see its status and result.</div>`}
        </div>
      </div>
    ` : `<div class="operator-job-empty">${candidateId ? "This Candidate has not been tried yet." : "No Candidate tries have been started for this Run."}</div>`}
  `;
}

function renderOperatorJobRow(job, selectedJobId = state.selectedOperatorJobId) {
  const target = job.target || {};
  const selected = job.job_id === selectedJobId;
  return `
    <button class="operator-job-row ${selected ? "selected" : ""}" data-operator-job-id="${escapeHtml(job.job_id)}" type="button">
      <span class="operator-job-row-heading">
        <strong>${escapeHtml(operatorJobLabel(job))}</strong>
        ${statusPill(job.state)}
      </span>
      <span class="operator-job-target" title="${escapeHtml(target.candidate_id || "")}">${escapeHtml(target.candidate_id || "candidate")}</span>
      <span class="operator-job-time">${escapeHtml(formatRealmTime(job.updated_at || job.created_at) || "time unavailable")}</span>
    </button>
  `;
}

function operatorJobLabel(job) {
  if (job && job.job_kind === "candidate-debug-run") return "Run headless";
  if (job && job.job_kind === "environment-preview") return "Open interactive interface";
  return "Candidate try";
}

function renderOperatorJobSummary(job) {
  const target = job.target || {};
  const outcome = job.outcome || {};
  const result = job.result || null;
  const executionPolicy = job.execution_policy || {};
  const networkPolicy = String(executionPolicy.network_policy || "");
  const networkEnforcement = String(executionPolicy.network_enforcement || "");
  const stopPending = state.pendingOperatorJobStops.has(job.job_id);
  const canStop = operatorJobIsActive(job) && Boolean(job.can_stop);
  const reconciliation = String(job.reconciliation_state || "");
  const cleanupState = String(job.cleanup_state || "");
  return `
    <div class="operator-job-detail-heading">
      <div>
        <span class="eyebrow">${escapeHtml(operatorJobLabel(job))}</span>
        <strong title="${escapeHtml(job.job_id)}">${escapeHtml(target.candidate_id || job.job_id)}</strong>
      </div>
      <div class="detail-actions">
        ${statusPill(job.state)}
        ${canStop ? `<button class="danger-button compact-action stop-operator-job" data-stop-operator-job="${escapeHtml(job.job_id)}" type="button" ${stopPending ? "disabled" : ""}>${stopPending ? "Stopping…" : "Stop"}</button>` : ""}
      </div>
    </div>
    <div class="operator-job-notice">This try does not use the Run's trial budget or change its recorded results, ranking, or best Candidate.</div>
    ${renderCandidateInspectionPlan(job.inspection_plan, { result: true })}
    ${job.stop_requested && operatorJobIsActive(job) ? `<div class="operator-job-notice">Stop requested; waiting for this try to stop.</div>` : ""}
    ${cleanupState === "pending" ? `<div class="operator-job-notice warning" role="status">The result is saved. OptPilot is finishing cleanup now; if interrupted, it will resume automatically.</div>` : ""}
    ${["unconfirmed", "degraded"].includes(reconciliation) ? `<div class="operator-job-notice warning" role="status">Cancellation or cleanup is ${escapeHtml(reconciliation)}. This try remains visible until cleanup is confirmed.</div>` : ""}
    ${networkEnforcement === "advisory" ? `<div class="operator-job-notice warning" role="status">Network access is ${escapeHtml(networkPolicy || "unspecified")}, but isolation could not be fully enforced for this try.</div>` : ""}
    ${state.operatorJobStopErrors[job.job_id] ? `<div class="operator-job-notice error" role="alert">Could not stop this try: ${escapeHtml(state.operatorJobStopErrors[job.job_id])}</div>` : ""}
    ${state.operatorJobDetailError && job.job_id === state.selectedOperatorJobId ? `<div class="operator-job-notice error" role="alert">Could not refresh this try: ${escapeHtml(state.operatorJobDetailError)}</div>` : ""}
    ${renderOperatorJobReviewAction(job)}
    ${renderOperatorJobPresentation(job)}
    ${renderOperatorJobInterfaceOutputs(job)}
    ${result ? renderOperatorJobResult(result, { includeOutputs: false }) : `<div class="operator-job-pending-result">${operatorJobIsActive(job) ? "Waiting for results." : "This try has no saved result."}</div>`}
    <details class="operator-job-more">
      <summary>More</summary>
      <dl class="operator-job-facts">
        <div><dt>Status</dt><dd>${escapeHtml(job.state || "unknown")}</dd></div>
        <div><dt>Candidate</dt><dd>${escapeHtml(target.candidate_id || "-")}</dd></div>
        <div><dt>Cleanup check</dt><dd>${escapeHtml(reconciliation || "-")}</dd></div>
        <div><dt>Cleanup</dt><dd>${escapeHtml(cleanupState || "-")}</dd></div>
        <div><dt>Updated</dt><dd>${escapeHtml(formatRealmTime(job.updated_at || job.created_at) || "-")}</dd></div>
        ${networkPolicy ? `<div><dt>Network access</dt><dd>${escapeHtml(networkPolicy)}${networkEnforcement ? ` (${escapeHtml(networkEnforcement)})` : ""}</dd></div>` : ""}
        ${outcome.status ? `<div><dt>Result status</dt><dd>${escapeHtml(outcome.status)}</dd></div>` : ""}
        ${outcome.code ? `<div><dt>Result code</dt><dd>${escapeHtml(outcome.code)}</dd></div>` : ""}
      </dl>
    </details>
  `;
}

function renderOperatorJobReviewAction(job) {
  if (!job || !["candidate-debug-run", "environment-preview"].includes(job.job_kind) || !["succeeded", "failed", "cancelled"].includes(job.state)) return "";
  const candidateId = String(job.target && job.target.candidate_id || "");
  const reviewItem = reviewItemForCandidate(candidateId);
  const attached = reviewHasOperatorJob(reviewItem, job.job_id);
  const pending = state.reviewPendingOperatorJobIds.has(job.job_id);
  const error = state.reviewOperatorJobErrors[job.job_id];
  const candidate = workbenchPage(state.selectedRun, "candidate").items.find((item) => item && item.id === candidateId);
  if (reviewItem) {
    return `
      <div class="operator-job-review-action">
        <button class="${attached ? "ghost-button" : "primary-button"} compact-action" data-attach-job-to-review="${escapeHtml(job.job_id)}" type="button" ${attached || pending ? "disabled" : ""}>${escapeHtml(attached ? "Saved to Shortlist" : pending ? "Saving…" : "Save try result to Shortlist")}</button>
        <span>${escapeHtml(attached ? "This finished result is saved with the Candidate." : "Saves this result while preserving your pending Shortlist notes and order.")}</span>
        ${error ? `<div class="selection-action-error" role="alert">${escapeHtml(error)} <button class="ghost-button compact-action" data-attach-job-to-review="${escapeHtml(job.job_id)}" type="button">Try again</button></div>` : ""}
      </div>
    `;
  }
  if (candidate && candidate.selection && candidate.selection.selection_id) {
    return `
      <div class="operator-job-review-action">
        <button class="primary-button compact-action" data-add-job-to-review="${escapeHtml(job.job_id)}" data-review-selection="${escapeHtml(candidate.selection.selection_id)}" type="button" ${pending ? "disabled" : ""}>${escapeHtml(pending ? "Saving…" : "Save Candidate and try result")}</button>
        <span>Saves the Candidate and this finished result together in the Shortlist.</span>
      </div>
    `;
  }
  return `<div class="operator-job-review-action"><span>Open this Candidate from the Candidates list and save it to the Shortlist before saving the try result.</span></div>`;
}

function renderOperatorJobPresentation(job) {
  if (!job || job.job_kind !== "environment-preview") return "";
  const presentation = job.presentation && typeof job.presentation === "object" ? job.presentation : {};
  const status = String(presentation.status || "pending");
  if (status === "available" && presentation.open_url) {
    return `
      <section class="operator-job-presentation" aria-label="Interactive Candidate interface">
        <div class="operator-job-presentation-heading">
          <div>
            <h4>Interactive interface</h4>
            <p>This interface uses the saved Candidate and selected Environment profile.</p>
          </div>
          <a class="ghost-button compact-action" href="${escapeHtml(presentation.open_url)}" target="_blank" rel="noopener noreferrer">Open larger</a>
        </div>
        <iframe title="Interactive Candidate interface" src="${escapeHtml(presentation.open_url)}" loading="eager" referrerpolicy="no-referrer" sandbox="allow-downloads allow-forms allow-modals allow-popups allow-same-origin allow-scripts"></iframe>
      </section>
    `;
  }
  if (status === "closed") {
    return `<div class="operator-job-notice">The interactive view is closed.</div>`;
  }
  if (status === "reconciling") {
    return `<div class="operator-job-notice warning" role="status">The Environment is running, but the interactive view is reconnecting.</div>`;
  }
  return `<div class="operator-job-notice" role="status">Preparing the interactive view…</div>`;
}

function renderOperatorJobInterfaceOutputs(job) {
  if (!job) return "";
  const bundle = job.interface_outputs && typeof job.interface_outputs === "object"
    ? job.interface_outputs
    : {};
  if (bundle.supported === false) return "";
  const lifecycle = String(bundle.lifecycle || "pending");
  const outputs = operatorJobOutputsForRender(job);
  const diagnostics = Array.isArray(bundle.diagnostics)
    ? bundle.diagnostics.filter((item) => item && item.source !== "generation").slice(0, 8)
    : [];
  const hasWorkspaceOutput = outputs.some((output) => {
    const keep = output && output.actions && output.actions.keep_as_workspace;
    return keep && keep.supported !== false;
  });
  const guidance = lifecycle === "retained"
    ? hasWorkspaceOutput
      ? "View read-only result files saved with this try. Output folders can also be saved as editable Workspaces."
      : "View read-only files saved with this try. They stay with this try unless you save an output folder as a Workspace."
    : lifecycle === "live"
      ? "Outputs are saved while the interactive try runs and become available when it finishes."
      : job.job_kind === "environment-preview"
        ? "Output collection starts with the interactive try."
        : "Declared outputs become available when this try finishes.";
  return `
    <section class="interface-output-section operator-job-interface-outputs" aria-label="Candidate try outputs">
      <div class="interface-output-heading">
        <div>
          <strong>Outputs</strong>
          <span>${escapeHtml(guidance)}</span>
        </div>
      </div>
      <div class="interface-output-list">
        ${renderInterfaceOutputList(outputs)}
      </div>
      ${diagnostics.length ? `<div class="operator-job-notice warning" role="status">${escapeHtml(diagnostics.map((item) => item.code || "output_capture_warning").join(", "))}</div>` : ""}
    </section>
  `;
}

function renderOperatorJobResult(result, options = {}) {
  const metrics = result.metrics && typeof result.metrics === "object" ? Object.entries(result.metrics).slice(0, 32) : [];
  const outputs = Array.isArray(result.declared_outputs) ? result.declared_outputs.slice(0, 32) : [];
  const logs = Array.isArray(result.logs) ? result.logs.slice(0, 8) : [];
  const includeOutputs = options.includeOutputs !== false;
  return `
    <div class="operator-job-result-heading">
      <h4>Result</h4>
      ${statusPill(result.status || "available")}
    </div>
    <section class="operator-job-result-group">
      <h5>Metrics</h5>
      ${metrics.length ? `<dl class="operator-job-metrics">${metrics.map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${formatMetric(value)}</dd></div>`).join("")}</dl>` : `<p class="muted-text">No metrics were returned.</p>`}
    </section>
    ${includeOutputs ? `<section class="operator-job-result-group">
      <h5>Declared outputs</h5>
      ${outputs.length ? `<div class="operator-job-output-list">${outputs.map(renderOperatorJobOutput).join("")}</div>` : `<p class="muted-text">No outputs were declared.</p>`}
    </section>` : ""}
    ${logs.length ? `<section class="operator-job-result-group"><h5>Log summary</h5><div class="operator-job-log-list">${logs.map((log) => `<span class="tag">${escapeHtml(log.stream || "log")} · ${escapeHtml(log.line_count ?? 0)} lines${log.truncated ? " · truncated" : ""}</span>`).join("")}</div></section>` : ""}
  `;
}

function renderOperatorJobOutput(output) {
  return `
    <div class="operator-job-output">
      <strong>${escapeHtml(output.declaration_id || "output")}</strong>
      <span>${escapeHtml(output.kind || "artifact")} · ${escapeHtml(formatBytes(output.size_bytes || 0))}</span>
      ${output.media_type ? `<span>${escapeHtml(output.media_type)}</span>` : ""}
    </div>
  `;
}

function bindOperatorJobEvents() {
  if (!els.runDetail) return;
  els.runDetail.querySelectorAll("[data-operator-job-id]").forEach((button) => {
    button.addEventListener("click", () => selectOperatorJob(button.dataset.operatorJobId));
  });
  els.runDetail.querySelectorAll("[data-stop-operator-job]").forEach((button) => {
    button.addEventListener("click", () => stopOperatorJob(button.dataset.stopOperatorJob));
  });
  els.runDetail.querySelectorAll("[data-attach-job-to-review]").forEach((button) => {
    button.addEventListener("click", () => attachOperatorJobToReview(button.dataset.attachJobToReview));
  });
  els.runDetail.querySelectorAll("[data-add-job-to-review]").forEach((button) => {
    button.addEventListener("click", () => addCandidateToReview(button.dataset.reviewSelection, { operatorJobId: button.dataset.addJobToReview }));
  });
  const retry = els.runDetail.querySelector(".operator-jobs-retry");
  if (retry) retry.addEventListener("click", () => loadSelectedRunOperatorJobs());
  const jobId = state.selectedOperatorJobId;
  bindInterfaceOutputControls(els.runDetail, {
    view: (outputId) => viewOperatorJobOutput(jobId, outputId),
    keep: (outputId) => keepOperatorJobOutput(jobId, outputId),
    retry: (outputId) => retryOperatorJobOutput(jobId, outputId),
  });
}

function runTabContent(detail) {
  if (state.activeRunTab === "overview") {
    return runOverview(detail);
  }
  if (state.activeRunTab === "timeline") return renderRunTimeline(detail);
  if (state.activeRunTab === "review") return renderReviewCollection(detail);
  return renderWorkbenchPage(detail, state.activeRunTab);
}

function canonicalRunId(run) {
  return String(run && (run.run_id || run.id) || "");
}

function runRowKey(run) {
  return canonicalRunId(run);
}

function runStatus(run) {
  return String(run && (run.run_status || run.status || run.state) || "unknown");
}

function runObjective(run) {
  const objective = run && run.objective || {};
  return {
    name: String(objective.name || objective.metric || ""),
    direction: String(objective.direction || ""),
  };
}

function runCounts(run) {
  const counts = run && run.counts || {};
  const logical = counts.logical_trials || {};
  const attempts = counts.attempts || {};
  const observations = counts.observations || {};
  return {
    candidates: Number(counts.candidates ?? (run && run.candidate_count) ?? 0),
    acceptedTrials: Number(logical.total ?? (run && (run.accepted_trials ?? run.completed_trials)) ?? 0),
    terminalTrials: Number(logical.terminal ?? (run && (run.terminal_trials ?? run.completed_trials)) ?? 0),
    successfulTrials: Number(logical.successful ?? (run && run.successful_trials) ?? 0),
    finalFailures: Number(logical.final_failures ?? (run && (run.final_failure_count ?? run.failure_count)) ?? 0),
    attempts: Number(attempts.total ?? (run && run.attempt_count) ?? 0),
    retries: Number(attempts.retries ?? (run && run.retry_count) ?? 0),
    observations: Number(observations.total ?? (run && run.observation_count) ?? 0),
  };
}

function runPlannedWork(run) {
  const budget = run && run.budget && typeof run.budget === "object" ? run.budget : {};
  const completed = runCounts(run).terminalTrials;
  if (!Object.prototype.hasOwnProperty.call(budget, "max_trials")) {
    return completed
      ? `${completed} finished · plan unavailable`
      : "Planned work unavailable";
  }
  const planned = budget.max_trials;
  if (planned == null) return `${completed} finished · no trial limit`;
  const numeric = Number(planned);
  if (!Number.isFinite(numeric) || numeric < 0) return "Planned work unavailable";
  const total = Math.trunc(numeric);
  return `${completed}/${total} ${total === 1 ? "trial" : "trials"} finished`;
}

function runBestPrimaryValue(run) {
  const objective = runObjective(run);
  const label = objective.name || "primary metric";
  const best = run && run.best_comparable_candidate && typeof run.best_comparable_candidate === "object"
    ? run.best_comparable_candidate
    : {};
  return {
    label,
    available: best.available === true,
    reason: String(best.reason || (best.available === true ? "" : "overview_unavailable")),
    candidateId: String(best.candidate_id || ""),
    value: best.value ?? null,
  };
}

function renderRunStopConfirmation() {
  const pending = state.pendingRunStop;
  if (!pending || !els.runStopModal) {
    closeRunStopConfirmation();
    return;
  }
  if (els.runStopTitle) els.runStopTitle.textContent = `Stop ${pending.name || "this Run"}?`;
  if (els.runStopBody) {
    els.runStopBody.innerHTML = `
      <p class="run-stop-identity">Run <code>${escapeHtml(pending.runId)}</code></p>
      <ul class="run-stop-effects">
        <li><strong>Remains available:</strong> completed Candidates, trials, metrics, and other results already recorded in this Run.</li>
        <li><strong>Stops:</strong> only future Candidate generation and evaluation work for this Run.</li>
      </ul>
    `;
  }
  if (els.runStopError) {
    els.runStopError.textContent = pending.error || "";
    els.runStopError.hidden = !pending.error;
  }
  if (els.runStopCancelButton) els.runStopCancelButton.disabled = pending.submitting;
  if (els.runStopSubmitButton) {
    els.runStopSubmitButton.disabled = pending.submitting;
    els.runStopSubmitButton.textContent = pending.submitting ? "Stopping…" : "Stop Run";
  }
  els.runStopModal.hidden = false;
}

function openRunStopConfirmation(run, trigger) {
  const runId = canonicalRunId(run);
  if (!runId || !els.runStopModal) return;
  state.runStopReturnFocus = trigger || null;
  state.pendingRunStop = {
    runId,
    name: String(run.name || ""),
    requestId: newRequestId(),
    submitting: false,
    error: "",
  };
  renderRunStopConfirmation();
  if (els.runStopSubmitButton) els.runStopSubmitButton.focus();
}

function closeRunStopConfirmation(options = {}) {
  const returnFocus = state.runStopReturnFocus;
  const runId = state.pendingRunStop && state.pendingRunStop.runId;
  state.pendingRunStop = null;
  state.runStopReturnFocus = null;
  if (els.runStopModal) els.runStopModal.hidden = true;
  if (els.runStopError) {
    els.runStopError.textContent = "";
    els.runStopError.hidden = true;
  }
  if (options.restoreFocus !== false) {
    window.requestAnimationFrame(() => {
      const fallback = state.selectedRunId === runId
        ? els.runDetail && els.runDetail.querySelector(".stop-selected-run")
        : null;
      const target = returnFocus && returnFocus.isConnected ? returnFocus : fallback;
      if (target && typeof target.focus === "function") target.focus();
    });
  }
}

function handleRunStopConfirmationKeydown(event) {
  const pending = state.pendingRunStop;
  if (!pending || !els.runStopModal) return;
  if (event.key === "Escape" && !pending.submitting) {
    event.preventDefault();
    closeRunStopConfirmation();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...els.runStopModal.querySelectorAll(
    "button:not([disabled]), [tabindex]:not([tabindex='-1'])",
  )].filter((element) => !element.hidden && !element.closest("[hidden]"));
  if (!focusable.length) {
    event.preventDefault();
    if (els.runStopDialog) els.runStopDialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

async function confirmRunStop() {
  const pending = state.pendingRunStop;
  if (!pending || pending.submitting) return;
  pending.submitting = true;
  pending.error = "";
  renderRunStopConfirmation();
  try {
    const payload = await postJson(`/api/runs/${encodeURIComponent(pending.runId)}/cancel`, {
      schema: "optpilot.run-cancel-request.v1",
      request_id: pending.requestId,
    });
    if (!payload || payload.run_id !== pending.runId || !payload.launch) {
      throw new Error("Run cancellation response does not match this Run.");
    }
    const runId = pending.runId;
    closeRunStopConfirmation({ restoreFocus: false });
    await loadRunsAndJobs();
    if (state.selectedRunId === runId) {
      await loadRunDetail(runId, { keepTab: true });
    }
  } catch (error) {
    if (state.pendingRunStop !== pending) return;
    pending.submitting = false;
    pending.error = error && error.message ? error.message : "Run cancellation failed.";
    renderRunStopConfirmation();
  }
}

function exactRunOverview(detail) {
  const overview = detail && detail.workbench && detail.workbench.overview;
  return overview && overview.schema === "optpilot.run-overview-projection.v1"
    ? overview
    : null;
}

function runOverviewBest(detail) {
  const overview = exactRunOverview(detail);
  const best = overview && overview.best_candidate || {};
  return {
    available: best.available === true,
    reason: String(best.reason || "overview_unavailable"),
    candidateId: String(best.candidate_id || ""),
    value: best.value ?? null,
    sampleCount: best.sample_count ?? null,
    rank: best.rank ?? null,
    tieCount: Number(best.tie_count || 0),
    evaluationPlanGroup: best.evaluation_plan_group ?? null,
  };
}

function runHeadlineResult(detail) {
  const best = runOverviewBest(detail);
  if (best.available) {
    return {
      label: "Best comparable result",
      value: formatMetric(best.value),
      candidateId: best.candidateId,
      sampleCount: best.sampleCount,
    };
  }
  const overview = exactRunOverview(detail);
  const series = overview && overview.objective_series || {};
  const points = Array.isArray(series.points)
    ? series.points.filter((point) => point && typeof point.value === "number" && Number.isFinite(point.value))
    : [];
  if (points.length === 1) {
    return {
      label: "Only complete result",
      value: formatMetric(points[0].value),
      candidateId: String(points[0].candidate_id || ""),
      sampleCount: points[0].sample_count ?? null,
    };
  }
  if (points.length > 1) {
    return {
      label: "Comparison",
      value: "Not comparable",
      candidateId: "",
      sampleCount: null,
    };
  }
  return { label: "Result", value: "Not available", candidateId: "", sampleCount: null };
}

function runCompletionMessage(summary, status) {
  const stopCode = String(summary && summary.stop_code || "");
  if (["failed"].includes(status)) return "This Run stopped before it could finish.";
  if (["cancelled", "canceled"].includes(status)) return "This Run was stopped. Results already recorded are still available.";
  if (["completed", "succeeded"].includes(status) && stopCode === "max_trials") {
    return "This Run finished its planned trials.";
  }
  return "";
}

function runOverviewBestReason(reason) {
  const messages = {
    waiting_for_first_candidate: "Waiting for the method to submit its first Candidate.",
    no_complete_candidate_yet: "No Candidate has finished all of its planned trials yet.",
    all_evaluations_failed: "All terminal evaluations failed, so there is no complete Candidate result to compare.",
    run_finished_without_complete_candidate: "The Run finished before any Candidate completed all planned trials.",
    only_one_complete_candidate: "Only one Candidate has a complete result. At least two Candidates tested with the same settings are needed before OptPilot can rank them.",
    complete_candidates_use_different_evaluation_plans: "Complete Candidates used different trial settings, so OptPilot will not rank them against one another.",
    no_comparable_complete_candidate: "No complete Candidates were tested with matching settings.",
    overview_unavailable: "The complete Candidate summary is unavailable for this Run.",
  };
  return messages[reason] || "No comparable complete Candidate is available for this Run.";
}

function runHead(run) {
  const head = run && (run.head || run.cursor);
  if (!head || head.revision == null || head.sequence == null) return null;
  return { revision: Number(head.revision), sequence: Number(head.sequence) };
}

function sameRunHead(left, right) {
  const a = runHead({ head: left });
  const b = runHead({ head: right });
  return Boolean(a && b && a.revision === b.revision && a.sequence === b.sequence);
}

function coherentRunDetail(detail, expectedRunId) {
  if (!detail || !detail.run || !detail.workbench || !detail.workbench.head) return false;
  const summary = detail.workbench.summary || {};
  if (canonicalRunId(detail.run) !== expectedRunId || summary.run_id !== expectedRunId) return false;
  if (!sameRunHead(detail.workbench.head, summary.cursor)) return false;
  const comparability = detail.workbench.comparability;
  if (
    !comparability
    || comparability.schema !== "optpilot.run-comparability-projection.v1"
    || comparability.run_id !== expectedRunId
    || !sameRunHead(comparability.head, detail.workbench.head)
  ) return false;
  const overview = detail.workbench.overview;
  if (
    !overview
    || overview.schema !== "optpilot.run-overview-projection.v1"
    || overview.run_id !== expectedRunId
    || !sameRunHead(overview.head, detail.workbench.head)
  ) return false;
  const pages = detail.pages && typeof detail.pages === "object" ? Object.values(detail.pages) : [];
  if (pages.some((page) => !page || !sameRunHead(page.head, detail.workbench.head))) return false;
  if (detail.timeline && !sameRunHead(detail.timeline.head, detail.workbench.head)) return false;
  const candidateResolution = detail.candidate_resolution;
  if (
    candidateResolution
    && (
      candidateResolution.schema !== "optpilot.run-candidate-resolution.v1"
      || candidateResolution.run_id !== expectedRunId
      || !sameRunHead(candidateResolution.head, detail.workbench.head)
    )
  ) return false;
  return true;
}

function runWorkbenchTabs(detail = state.selectedRun) {
  const tabs = [
    ["overview", "Overview"],
    ["candidate", "Candidates"],
  ];
  const shortlist = reviewCollection(detail);
  if (shortlist) {
    tabs.push(["review", "Shortlist"]);
  }
  return tabs;
}

function runTabDomId(tab) {
  return `run-result-tab-${String(tab || "section").replace(/[^a-z0-9_-]/gi, "-")}`;
}

function runTabButtonHtml(tab, label, detail = state.selectedRun) {
  const active = state.activeRunTab === tab;
  const primaryTabs = runWorkbenchTabs(detail);
  const hasActivePrimaryTab = primaryTabs.some(([candidate]) => candidate === state.activeRunTab);
  const keyboardAnchor = active || (!hasActivePrimaryTab && primaryTabs[0] && primaryTabs[0][0] === tab);
  const tabSemantics = hasActivePrimaryTab
    ? `role="tab" aria-selected="${active ? "true" : "false"}" aria-controls="run-result-tabpanel" tabindex="${keyboardAnchor ? "0" : "-1"}"`
    : "";
  return `<button id="${escapeHtml(runTabDomId(tab))}" class="tab ${active ? "active" : ""}" data-run-tab="${escapeHtml(tab)}" type="button" ${tabSemantics}>${escapeHtml(label)}</button>`;
}

function runTabPanelHtml(detail) {
  const activePrimaryTab = runWorkbenchTabs(detail).find(([tab]) => tab === state.activeRunTab);
  const accessibility = activePrimaryTab
    ? `role="tabpanel" aria-labelledby="${escapeHtml(runTabDomId(activePrimaryTab[0]))}"`
    : 'role="region" aria-label="Run technical details"';
  return `<div id="run-result-tabpanel" class="tab-content" ${accessibility} tabindex="0">${runTabContent(detail)}</div>`;
}

function activateRunTab(tab, options = {}) {
  state.activeRunTab = tab;
  if (state.activeRunTab !== "candidate" && state.routedCandidateId) {
    state.routedCandidateId = null;
    state.routedCandidateResolution = null;
    state.routedCandidateFocusApplied = "";
    syncStudioRoute();
  }
  renderRunDetail();
  if (options.restoreFocus) {
    window.requestAnimationFrame(() => {
      const activeTab = document.getElementById(runTabDomId(tab));
      if (activeTab) activeTab.focus();
    });
  }
}

function handleRunTablistKeydown(event) {
  if (!event.currentTarget || !event.target.closest) return;
  const current = event.target.closest('[role="tab"]');
  if (!current || !event.currentTarget.contains(current)) return;
  const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]:not([disabled])')];
  const index = tabs.indexOf(current);
  if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === "Home"
    ? tabs[0]
    : event.key === "End"
    ? tabs[tabs.length - 1]
    : tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  activateRunTab(next.dataset.runTab, { restoreFocus: true });
}

function runTechnicalTabs() {
  return [
    ["logical_trial", "Trials"],
    ["attempt", "Trial attempts"],
    ["observation", "Trial results"],
    ["artifact", "Saved files"],
    ["timeline", "Event history"],
  ];
}

function workbenchPage(detail, kind) {
  const page = detail && detail.pages && detail.pages[kind];
  return page && typeof page === "object" ? page : { items: [], page: {} };
}

function reviewDraft(detail = state.selectedRun) {
  const runId = detail && detail.run && canonicalRunId(detail.run);
  const collection = reviewCollection(detail);
  if (!runId || !collection) return null;
  const current = state.reviewDrafts[runId];
  if (!current || current.base_revision_digest !== collection.revision_digest) {
    state.reviewDrafts[runId] = {
      collection_id: String(collection.collection_id || ""),
      expected_revision: Number(collection.revision || 0),
      base_revision_digest: String(collection.revision_digest || ""),
      title: String(collection.title || "Shortlist"),
      items: (Array.isArray(collection.items) ? collection.items : []).map((item) => ({
        selection_digest: String(item && item.selection && item.selection.selection_digest || ""),
        note: String(item && item.note || ""),
        inspection_outcomes: Array.isArray(item && item.inspection_outcomes) ? item.inspection_outcomes : [],
        selection: item && item.selection || {},
        evidence: item && item.evidence || {},
      })),
      dirty: false,
    };
  }
  return state.reviewDrafts[runId];
}

function reviewCollectionHistory(detail = state.selectedRun) {
  const value = detail && detail.review_collection_history;
  return value && typeof value === "object" ? value : null;
}

function displayedReviewCollection(detail = state.selectedRun) {
  const current = reviewCollection(detail);
  const runId = detail && detail.run && canonicalRunId(detail.run);
  if (!current || !runId) return current;
  const viewed = state.reviewViewedCollections[runId];
  if (
    viewed
    && viewed.collection_id === current.collection_id
    && Number(viewed.revision) !== Number(current.revision)
  ) return viewed;
  return current;
}

function reviewCandidateResult(item) {
  const result = item && item.evidence && item.evidence.candidate_result || {};
  const aggregate = result.aggregate && typeof result.aggregate === "object" ? result.aggregate : null;
  const objective = result.objective || {};
  return {
    metric: String(objective.metric || "objective"),
    value: aggregate ? aggregate.value : null,
    rank: result.comparison && Number.isInteger(result.comparison.rank) ? result.comparison.rank : null,
    finality: String(result.comparison && result.comparison.finality || ""),
  };
}

function reviewItemForCandidate(candidateId, detail = state.selectedRun) {
  const draft = reviewDraft(detail);
  const items = Array.isArray(draft && draft.items) ? draft.items : [];
  return items.find((item) => item && item.selection && item.selection.entity_id === candidateId) || null;
}

function reviewHasOperatorJob(item, jobId) {
  const outcomes = Array.isArray(item && item.inspection_outcomes) ? item.inspection_outcomes : [];
  return outcomes.some((outcome) => outcome && outcome.schema === "optpilot.review-inspection-outcome.v1" && outcome.operator_job_id === jobId);
}

function renderReviewCollection(detail) {
  const current = reviewCollection(detail);
  if (!current) {
    return `
      <section class="review-collection-empty">
        <div class="empty-state">
          <h3>No saved Candidates yet</h3>
          <p>Open Candidates and choose <strong>Save to Shortlist</strong>. This saves the Candidate and its recorded result; it does not create a Workspace or run the Candidate.</p>
        </div>
      </section>
    `;
  }
  const collection = displayedReviewCollection(detail);
  const historical = Number(collection.revision) !== Number(current.revision);
  const draft = historical ? null : reviewDraft(detail);
  const items = historical ? (Array.isArray(collection.items) ? collection.items : []) : draft ? draft.items : [];
  const history = reviewCollectionHistory(detail);
  const revisions = Array.isArray(history && history.items) ? history.items : [];
  const error = state.reviewError;
  return `
    <section class="review-collection" data-review-collection="${escapeHtml(collection.collection_id || "")}">
      <div class="review-collection-heading">
        <div>
          <span class="eyebrow">Saved Candidates</span>
          <h3>${escapeHtml(collection.title || "Shortlist")}</h3>
          <p>Keep promising Candidates, notes, and selected try results together for this Run.</p>
        </div>
        <div class="tag-row">
          <span class="tag status-ready">${historical ? "Earlier saved version" : "Saved"}</span>
        </div>
      </div>
      ${error ? `<p class="selection-action-error" role="alert">${escapeHtml(error)}</p>` : ""}
      <label class="review-title-field">
        <span>Shortlist name</span>
        <input type="text" data-review-title value="${escapeHtml(draft && draft.title || collection.title || "Shortlist")}" maxlength="512" ${historical ? "readonly" : ""} />
      </label>
      <div class="review-shortlist">
        ${items.map((item, index) => renderReviewItem(item, index, items.length, { readOnly: historical })).join("") || `<div class="empty-inline"><strong>This saved Shortlist is empty.</strong><span>${historical ? "This is the exact saved version." : "Add another Candidate or save this version to record the cleared Shortlist."}</span></div>`}
      </div>
      <div class="review-collection-actions">
        ${historical ? "" : `<button class="primary-button review-save" type="button" ${state.reviewSavePending || state.reviewDeletePending || !draft || !draft.dirty ? "disabled" : ""}>${state.reviewSavePending ? "Saving…" : "Save changes"}</button>`}
        <span>${historical ? "Viewing an earlier saved version. Your current edits remain unchanged." : draft && draft.dirty ? "You have unsaved changes." : "All changes are saved."}</span>
      </div>
      <details class="review-more">
        <summary>More</summary>
        <div class="review-history-bar">
          <label>
            <span>Version history</span>
            <select data-review-revision ${state.reviewHistoryPending ? "disabled" : ""}>
              ${revisions.map((item) => `<option value="${escapeHtml(item.revision)}" ${Number(item.revision) === Number(collection.revision) ? "selected" : ""}>Saved version ${escapeHtml(item.revision)}${Number(item.revision) === Number(current.revision) ? " (current)" : ""} · ${escapeHtml(item.item_count)} Candidate${Number(item.item_count) === 1 ? "" : "s"}</option>`).join("")}
            </select>
          </label>
          ${history && history.page && history.page.has_more ? `<button class="ghost-button compact-action review-history-more" type="button" ${state.reviewHistoryPending ? "disabled" : ""}>${state.reviewHistoryPending ? "Loading…" : "Load older history"}</button>` : ""}
          ${historical ? `<button class="ghost-button compact-action review-history-current" type="button">Return to current</button>` : ""}
          <button class="ghost-button review-export" type="button">Export this saved version</button>
          ${historical ? "" : `<button class="danger-button review-delete" type="button" ${state.reviewSavePending || state.reviewDeletePending ? "disabled" : ""}>${state.reviewDeletePending ? "Deleting…" : "Delete Shortlist"}</button>`}
        </div>
      </details>
    </section>
  `;
}

function renderReviewItem(item, index, count, options = {}) {
  const selection = item && item.selection || {};
  const result = reviewCandidateResult(item);
  const retention = item && item.evidence && item.evidence.retention || {};
  const artifactCount = Number(retention.artifact_content_count || 0);
  return `
    <article class="review-item" data-review-index="${escapeHtml(index)}">
      <div class="review-item-order"><strong>${escapeHtml(index + 1)}</strong><span>of ${escapeHtml(count)}</span></div>
      <div class="review-item-main">
        <div class="review-item-heading">
          <div>
            <span class="catalog-kind-chip">candidate</span>
            <strong title="${escapeHtml(selection.entity_id || "")}">${escapeHtml(selection.entity_id || "Candidate")}</strong>
          </div>
          <div class="tag-row">
            ${result.rank != null ? `<span class="tag">Rank #${escapeHtml(result.rank)}</span>` : ""}
            <span class="tag">${escapeHtml(result.metric)} ${result.value == null ? "not aggregated" : formatMetric(result.value)}</span>
            ${artifactCount ? `<span class="tag">${escapeHtml(artifactCount)} saved output${artifactCount === 1 ? "" : "s"}</span>` : ""}
            ${selection.entity_id ? `<button class="ghost-button compact-action" data-open-candidate-route="${escapeHtml(selection.entity_id)}" type="button">Open Candidate</button>` : ""}
          </div>
        </div>
        <label class="review-note-field">
          <span>Notes</span>
          <textarea data-review-note="${escapeHtml(index)}" rows="3" maxlength="65536" placeholder="What did you learn from the results, files, or interactive view?" ${options.readOnly ? "readonly" : ""}>${escapeHtml(item.note || "")}</textarea>
        </label>
        ${renderReviewInspectionOutcomes(item)}
      </div>
      ${options.readOnly ? "" : `<div class="review-item-controls" aria-label="Shortlist order">
        <button class="ghost-button compact-action" data-review-move="up" data-review-index="${escapeHtml(index)}" type="button" ${index === 0 ? "disabled" : ""}>Move up</button>
        <button class="ghost-button compact-action" data-review-move="down" data-review-index="${escapeHtml(index)}" type="button" ${index === count - 1 ? "disabled" : ""}>Move down</button>
        <button class="ghost-button compact-action destructive" data-review-remove="${escapeHtml(index)}" type="button">Remove</button>
      </div>`}
    </article>
  `;
}

function renderReviewInspectionOutcomes(item) {
  const outcomes = Array.isArray(item && item.inspection_outcomes) ? item.inspection_outcomes : [];
  if (!outcomes.length) return "";
  return `
    <section class="review-inspection-evidence" aria-label="Saved Candidate try results">
      <div class="review-inspection-heading">
        <strong>Saved try results</strong>
        <span>${escapeHtml(outcomes.length)} saved result${outcomes.length === 1 ? "" : "s"}</span>
      </div>
      <div class="review-inspection-list">${outcomes.map(renderReviewInspectionOutcome).join("")}</div>
    </section>
  `;
}

function renderReviewInspectionOutcome(outcome) {
  if (!outcome || outcome.schema !== "optpilot.review-inspection-outcome.v1") {
    return `<div class="review-inspection-row"><div><strong>Candidate try</strong><span>Result saved with this Shortlist version.</span></div><span class="tag">saved</span></div>`;
  }
  const terminal = outcome.outcome || {};
  const result = outcome.result || {};
  const metrics = result.metrics && result.metrics.values && typeof result.metrics.values === "object"
    ? Object.entries(result.metrics.values).slice(0, 4)
    : [];
  const outputs = Number(result.declared_outputs && result.declared_outputs.total || 0);
  return `
    <div class="review-inspection-row">
      <div>
        <strong>${escapeHtml(operatorJobLabel({ job_kind: outcome.job_kind }))}</strong>
        <span>${escapeHtml(terminal.code || terminal.status || "finished result")} · ${escapeHtml(formatRealmTime(outcome.completed_at) || "time unavailable")}</span>
        ${metrics.length ? `<span>${metrics.map(([name, value]) => `${escapeHtml(name)} ${formatMetric(value)}`).join(" · ")}</span>` : ""}
      </div>
      <div class="tag-row">
        ${statusPill(terminal.status || "saved")}
        ${outputs ? `<span class="tag">${escapeHtml(outputs)} output${outputs === 1 ? "" : "s"}</span>` : ""}
      </div>
    </div>
  `;
}

function loadedObservationItems(detail) {
  const page = workbenchPage(detail, "observation");
  return Array.isArray(page.items) ? page.items : [];
}

function observationMetricRows(item) {
  const data = item && item.data && typeof item.data === "object" ? item.data : {};
  const metrics = data.metrics && typeof data.metrics === "object" ? data.metrics : {};
  return Array.isArray(metrics.rows) ? metrics.rows : [];
}

function loadedObservationMetricNames(detail) {
  const names = new Set();
  loadedObservationItems(detail).forEach((item) => {
    observationMetricRows(item).forEach((row) => {
      const value = row && row.value;
      if (row && row.supported && row.name && (typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value)))) {
        names.add(String(row.name));
      }
    });
  });
  return Array.from(names).sort((left, right) => left.localeCompare(right));
}

function selectedObservationMetric(detail, runId) {
  const names = loadedObservationMetricNames(detail);
  if (!names.length) return { names, selected: "" };
  const requested = state.runMetricSelections[runId];
  const objective = runObjective(detail.workbench.summary || detail.run || {}).name;
  const selected = names.includes(requested)
    ? requested
    : names.includes(objective)
      ? objective
      : names[0];
  return { names, selected };
}

function metricPlotValue(value) {
  if (typeof value === "boolean") return value ? 1 : 0;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function runObservationMetricPanel(detail, runId) {
  const page = workbenchPage(detail, "observation");
  const items = loadedObservationItems(detail);
  const { names, selected } = selectedObservationMetric(detail, runId);
  if (!items.length) {
    return `
      <section class="run-observation-panel" aria-label="Metric observations">
        <div class="run-observation-heading"><div><h3>Metric observations</h3><p>No completed metric observations are available yet.</p></div></div>
      </section>
    `;
  }
  if (!selected) {
    return `
      <section class="run-observation-panel" aria-label="Metric observations">
        <div class="run-observation-heading"><div><h3>Metric observations</h3><p>The loaded observations contain no finite numeric or boolean metrics that the generic viewer can plot.</p></div><span class="tag">${escapeHtml(items.length)} loaded</span></div>
      </section>
    `;
  }
  const samples = [];
  let booleanSamples = 0;
  let projectedNamesTruncated = false;
  items.forEach((item, index) => {
    const data = item && item.data || {};
    const metrics = data.metrics || {};
    projectedNamesTruncated = projectedNamesTruncated || Boolean(metrics.truncated);
    const row = observationMetricRows(item).find((candidate) => candidate && candidate.name === selected && candidate.supported);
    if (!row) return;
    const plotValue = metricPlotValue(row.value);
    if (plotValue === null) return;
    if (typeof row.value === "boolean") booleanSamples += 1;
    samples.push({
      index,
      observationId: item.id || "observation",
      candidateId: data.candidate_id || "",
      rawValue: row.value,
      plotValue,
    });
  });
  if (!samples.length) {
    return `
      <section class="run-observation-panel" aria-label="Metric observations">
        <div class="run-observation-heading"><div><h3>Metric observations</h3><p>No loaded observation has a usable value for ${escapeHtml(selected)} yet.</p></div><span class="tag">${escapeHtml(items.length)} loaded</span></div>
      </section>
    `;
  }
  const width = 760;
  const height = 190;
  const padding = { left: 54, right: 18, top: 18, bottom: 34 };
  const values = samples.map((sample) => sample.plotValue);
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (minimum === maximum) {
    const expansion = minimum === 0 ? 1 : Math.max(Math.abs(minimum) * 0.1, 0.5);
    minimum -= expansion;
    maximum += expansion;
  }
  const x = (sample) => padding.left + (sample.index / Math.max(items.length - 1, 1)) * (width - padding.left - padding.right);
  const y = (sample) => padding.top + ((maximum - sample.plotValue) / (maximum - minimum)) * (height - padding.top - padding.bottom);
  const points = samples.map((sample) => `${x(sample).toFixed(2)},${y(sample).toFixed(2)}`).join(" ");
  const latest = samples[samples.length - 1];
  const loadedScope = page.page && page.page.has_more
    ? `Showing ${items.length} loaded observations; more are available.`
    : `Showing all ${items.length} observations returned in this Run update.`;
  return `
    <section class="run-observation-panel" aria-label="Metric observations">
      <div class="run-observation-heading">
        <div>
          <h3>Metric observations</h3>
          <p>${escapeHtml(loadedScope)} Values come from one consistent Run update.</p>
        </div>
        <label class="run-metric-control"><span>Metric</span><select class="run-metric-select" data-run-id="${escapeHtml(runId)}">${names.map((name) => `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}</select></label>
      </div>
      <div class="run-metric-summary">
        <div><span>Usable observations</span><strong>${escapeHtml(samples.length)} / ${escapeHtml(items.length)}</strong></div>
        <div><span>Loaded minimum</span><strong>${formatMetric(Math.min(...values))}</strong></div>
        <div><span>Loaded maximum</span><strong>${formatMetric(Math.max(...values))}</strong></div>
        <div><span>Latest usable value</span><strong>${latest ? formatMetric(latest.rawValue) : "-"}</strong></div>
      </div>
      <div class="run-metric-chart-wrap">
        <svg class="run-metric-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Loaded ${escapeHtml(selected)} observations">
          <line x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}" class="run-metric-axis"></line>
          <line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}" class="run-metric-axis"></line>
          <text x="${padding.left - 8}" y="${padding.top + 4}" text-anchor="end">${escapeHtml(formatMetric(maximum))}</text>
          <text x="${padding.left - 8}" y="${height - padding.bottom + 4}" text-anchor="end">${escapeHtml(formatMetric(minimum))}</text>
          <text x="${padding.left}" y="${height - 10}">first loaded</text>
          <text x="${width - padding.right}" y="${height - 10}" text-anchor="end">latest loaded</text>
          ${samples.length > 1 ? `<polyline points="${points}" class="run-metric-line"></polyline>` : ""}
          ${samples.map((sample) => `<circle cx="${x(sample).toFixed(2)}" cy="${y(sample).toFixed(2)}" r="4" class="run-metric-point"><title>${escapeHtml(sample.observationId)} · ${escapeHtml(sample.candidateId || "candidate unavailable")} · ${escapeHtml(String(sample.rawValue))}</title></circle>`).join("")}
        </svg>
      </div>
      <details class="run-metric-table">
        <summary>View plotted values as a table</summary>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Observation</th><th>Candidate</th><th>${escapeHtml(selected)}</th></tr></thead>
            <tbody>${samples.map((sample) => `<tr><td>${escapeHtml(sample.observationId)}</td><td>${escapeHtml(sample.candidateId || "-")}</td><td>${escapeHtml(String(sample.rawValue))}</td></tr>`).join("")}</tbody>
          </table>
        </div>
      </details>
      ${booleanSamples ? `<p class="run-observation-note">${escapeHtml(booleanSamples)} boolean metric sample${booleanSamples === 1 ? " is" : "s are"} plotted as false = 0 and true = 1.</p>` : ""}
      ${projectedNamesTruncated ? `<p class="run-observation-note">More metric names exist than this summary shows.</p>` : ""}
    </section>
  `;
}

function runObservationConstraintPanel(detail) {
  const items = loadedObservationItems(detail);
  const byName = new Map();
  let sourceTruncated = false;
  items.forEach((item) => {
    const data = item && item.data || {};
    const constraints = data.constraints && typeof data.constraints === "object" ? data.constraints : {};
    sourceTruncated = sourceTruncated || Boolean(constraints.truncated);
    const rows = Array.isArray(constraints.rows) ? constraints.rows : [];
    rows.forEach((row) => {
      if (!row || !row.name) return;
      const current = byName.get(row.name) || { name: row.name, satisfied: 0, violated: 0, unsupported: 0, observed: 0 };
      if (row.supported && typeof row.value === "boolean") {
        current.observed += 1;
        if (row.value) current.satisfied += 1;
        else current.violated += 1;
      } else {
        current.unsupported += 1;
      }
      byName.set(row.name, current);
    });
  });
  const allRows = Array.from(byName.values()).sort((left, right) => left.name.localeCompare(right.name));
  const rows = allRows.slice(0, 16);
  return `
    <section class="run-observation-panel run-constraint-summary" aria-label="Constraint observations">
      <div class="run-observation-heading">
        <div><h3>Constraint observations</h3><p>Boolean evaluator results from the same loaded observation page. True means satisfied; false means violated.</p></div>
        <span class="tag">${escapeHtml(rows.length)} shown</span>
      </div>
      ${rows.length ? `<div class="run-constraint-grid">${rows.map((row) => `
        <div class="run-constraint-card">
          <strong>${escapeHtml(row.name)}</strong>
          <span><b>${escapeHtml(row.satisfied)}</b> satisfied · <b>${escapeHtml(row.violated)}</b> violated</span>
          <small>${escapeHtml(row.observed)} boolean results · ${escapeHtml(Math.max(items.length - row.observed, 0))} loaded observations without a boolean result${row.unsupported ? ` · ${escapeHtml(row.unsupported)} unsupported` : ""}</small>
        </div>
      `).join("")}</div>` : `<p class="run-observation-empty">No boolean constraint results are present in the loaded observations.</p>`}
      ${allRows.length > rows.length || sourceTruncated ? `<p class="run-observation-note">More constraint names exist than this summary shows.</p>` : ""}
    </section>
  `;
}

function runCompleteObjectivePanel(detail) {
  const overview = exactRunOverview(detail);
  const series = overview && overview.objective_series || {};
  const objective = overview && overview.objective || {};
  const points = Array.isArray(series.points)
    ? series.points.filter((point) => point && typeof point.value === "number" && Number.isFinite(point.value))
    : [];
  const total = Number(series.total_complete_candidates || 0);
  const returned = Number(series.returned || points.length);
  if (!points.length) {
    return `
      <section class="run-observation-panel" aria-label="Complete Candidate objective results">
        <div class="run-observation-heading">
          <div>
            <h3>Complete Candidate results</h3>
            <p>No Candidate has a complete objective result yet.</p>
          </div>
        </div>
      </section>
    `;
  }
  const width = 760;
  const height = 190;
  const padding = { left: 54, right: 18, top: 18, bottom: 34 };
  const values = points.map((point) => point.value);
  const rawMinimum = series.summary && series.summary.minimum;
  const rawMaximum = series.summary && series.summary.maximum;
  let minimum = typeof rawMinimum === "number" && Number.isFinite(rawMinimum) ? rawMinimum : Math.min(...values);
  let maximum = typeof rawMaximum === "number" && Number.isFinite(rawMaximum) ? rawMaximum : Math.max(...values);
  if (minimum === maximum) {
    const expansion = minimum === 0 ? 1 : Math.max(Math.abs(minimum) * 0.1, 0.5);
    minimum -= expansion;
    maximum += expansion;
  }
  const x = (_point, index) => padding.left + (index / Math.max(points.length - 1, 1)) * (width - padding.left - padding.right);
  const y = (point) => padding.top + ((maximum - point.value) / (maximum - minimum)) * (height - padding.top - padding.bottom);
  const coordinates = points.map((point, index) => `${x(point, index).toFixed(2)},${y(point).toFixed(2)}`).join(" ");
  const planGroups = Number(overview.counts && overview.counts.candidates && overview.counts.candidates.comparison_groups || 0);
  const scope = series.truncated
    ? `Showing ${returned} of ${total} complete Candidates.`
    : total === 1
    ? "The one complete Candidate is shown."
    : `All ${total} complete Candidates are shown.`;
  return `
    <section class="run-observation-panel" aria-label="Complete Candidate objective results">
      <div class="run-observation-heading">
        <div>
          <h3>Complete Candidate results</h3>
          <p>${escapeHtml(scope)} These results come from this Run.</p>
        </div>
        <span class="tag">${escapeHtml(objective.metric || "objective")} · ${escapeHtml(objective.aggregation_mode || "aggregate")}</span>
      </div>
      <div class="run-metric-summary">
        <div><span>Complete Candidates</span><strong>${escapeHtml(total)}</strong></div>
        <div><span>Run minimum</span><strong>${formatMetric(series.summary && series.summary.minimum)}</strong></div>
        <div><span>Run maximum</span><strong>${formatMetric(series.summary && series.summary.maximum)}</strong></div>
        <div><span>Latest Candidate value</span><strong>${formatMetric(series.summary && series.summary.last_in_order)}</strong></div>
      </div>
      <div class="run-metric-chart-wrap">
        <svg class="run-metric-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Complete Candidate ${escapeHtml(objective.metric || "objective")} results">
          <line x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}" class="run-metric-axis"></line>
          <line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}" class="run-metric-axis"></line>
          <text x="${padding.left - 8}" y="${padding.top + 4}" text-anchor="end">${escapeHtml(formatMetric(maximum))}</text>
          <text x="${padding.left - 8}" y="${height - padding.bottom + 4}" text-anchor="end">${escapeHtml(formatMetric(minimum))}</text>
          <text x="${padding.left}" y="${height - 10}">first complete</text>
          <text x="${width - padding.right}" y="${height - 10}" text-anchor="end">last accepted</text>
          ${points.length > 1 ? `<polyline points="${coordinates}" class="run-metric-line"></polyline>` : ""}
          ${points.map((point, index) => `<circle cx="${x(point, index).toFixed(2)}" cy="${y(point).toFixed(2)}" r="4" class="run-metric-point"><title>${escapeHtml(point.candidate_id || "Candidate")} · ${escapeHtml(String(point.value))}</title></circle>`).join("")}
        </svg>
      </div>
      <details class="run-metric-table">
        <summary>View complete Candidate values as a table</summary>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Candidate</th><th>Order</th><th>${escapeHtml(objective.metric || "Objective")}</th><th>Trials included</th><th>Trial settings</th><th>Rank</th></tr></thead>
            <tbody>${points.map((point) => `<tr><td>${escapeHtml(point.candidate_id || "-")}</td><td>${escapeHtml(point.accepted_sequence ?? "-")}</td><td>${escapeHtml(String(point.value))}</td><td>${escapeHtml(point.sample_count ?? "-")}</td><td>${escapeHtml(point.evaluation_plan_group ?? "-")}</td><td>${point.comparison_eligible ? escapeHtml(point.rank ?? "-") : "not comparable"}</td></tr>`).join("")}</tbody>
          </table>
        </div>
      </details>
      ${planGroups > 1 ? `<p class="run-observation-note">These values use ${escapeHtml(planGroups)} different trial-setting groups. They are shown for inspection, but OptPilot does not rank them against one another.</p>` : ""}
      ${series.truncated ? `<p class="run-observation-note">${escapeHtml(series.omitted || 0)} complete Candidate${Number(series.omitted || 0) === 1 ? " is" : "s are"} omitted from the plotted sample; the summary values still cover the complete Run.</p>` : ""}
    </section>
  `;
}

function runOverview(detail) {
  const summary = detail.workbench.summary || detail.run || {};
  const overview = exactRunOverview(detail);
  const objective = overview && overview.objective || runObjective(summary);
  const best = runOverviewBest(detail);
  const headlineResult = runHeadlineResult(detail);
  const onlyCompleteCandidate = !best.available && Boolean(headlineResult.candidateId);
  const counts = runCounts(summary);
  const overviewCounts = overview && overview.counts || {};
  const candidateCounts = overviewCounts.candidates || {};
  const trialCounts = overviewCounts.logical_trials || {};
  const budget = summary.budget || {};
  const capabilityNote = runProviderCapabilityNote(detail.workbench);
  const status = runStatus(summary);
  const bestState = best.available
    ? ""
    : `<div class="run-result-state ${["completed", "succeeded", "failed", "cancelled"].includes(status) ? "warning" : ""}" role="status"><strong>${onlyCompleteCandidate ? "Only one complete Candidate" : `No comparable complete Candidate${["completed", "succeeded", "failed", "cancelled"].includes(status) ? "" : " yet"}`}</strong><span>${escapeHtml(runOverviewBestReason(best.reason))}</span></div>`;
  return `
    ${bestState}
    <section class="run-next-step" aria-label="What to do with this Run">
      <div>
        <h3>Run results</h3>
        <p>See the overall outcome here. Open Candidates to review individual results, try a Candidate, save it to the Shortlist, or edit its files in a Workspace.</p>
      </div>
      ${headlineResult.candidateId ? `<div class="run-next-step-actions"><button class="primary-button compact-action" data-open-candidate-route="${escapeHtml(headlineResult.candidateId)}" type="button">${best.available ? "Open best comparable Candidate" : "Open only complete Candidate"}</button></div>` : ""}
    </section>
    <div class="detail-grid run-overview-grid">
      ${kvPanel("Objective and result", [
        ["Objective", `${objective.metric || objective.name || "-"} ${objective.direction || ""}`.trim()],
        ["Aggregation", candidateAggregationLabel(objective.aggregation_mode) || objective.aggregation_mode || "-"],
        [best.available ? "Best comparable Candidate" : onlyCompleteCandidate ? "Only complete Candidate" : "Best comparable Candidate", best.candidateId || headlineResult.candidateId || "-"],
        ["Complete Candidate value", headlineResult.candidateId ? headlineResult.value : "-"],
        ["Trials included in result", headlineResult.sampleCount ?? "-"],
      ])}
      ${kvPanel("Progress", [
        ["Planned trials", trialCounts.planned ?? budget.max_trials ?? "unbounded"],
        ["Active trials", trialCounts.active ?? counts.acceptedTrials - counts.terminalTrials],
        ["Completed trials", trialCounts.terminal ?? counts.terminalTrials],
        ["Complete Candidates", candidateCounts.complete ?? 0],
        ["Failures requiring attention", (overview && overview.failure_count) ?? counts.finalFailures],
        ["Retries", counts.retries],
      ])}
    </div>
    <div class="run-observation-insights">
      ${runCompleteObjectivePanel(detail)}
    </div>
    <details class="run-technical-details">
      <summary>How OptPilot compares Candidates</summary>
      ${capabilityNote}
      ${runComparabilityPanel(detail.workbench.comparability)}
    </details>
  `;
}

function runComparabilityPanel(projection) {
  if (!projection || projection.schema !== "optpilot.run-comparability-projection.v1") {
    return `
      <section class="run-comparability-panel" aria-label="Run comparability">
        <div class="run-comparability-heading">
          <div>
            <h3>Comparability</h3>
            <p>Comparability information is unavailable for this Run update.</p>
          </div>
        </div>
      </section>
    `;
  }
  const fingerprints = projection.fingerprints || {};
  const environment = fingerprints.environment_evaluation || {};
  const objective = fingerprints.objective || {};
  const report = projection.reproducibility || {};
  const dimensions = report.dimensions || {};
  const automaticRanking = projection.automatic_ranking || {};
  const dimensionSpecs = [
    ["semantic_inputs", "Semantic inputs"],
    ["bytes_available_now", "Bytes available now"],
    ["runtime_identity", "Runtime identity"],
    ["runtime_available_now", "Runtime available now"],
    ["isolation", "Isolation"],
    ["external_replayability", "External replayability"],
    ["seed_repetition_plan", "Seed and repetition plan"],
    ["terminal_evidence", "Terminal evidence"],
  ];
  const dimensionRows = dimensionSpecs.map(([key, label]) => {
    const dimension = dimensions[key] && typeof dimensions[key] === "object"
      ? dimensions[key]
      : {};
    const status = typeof dimension.status === "string" ? dimension.status : "not_reported";
    const reason = typeof dimension.reason === "string" ? dimension.reason : "No reason was reported.";
    return `
      <div class="run-comparability-dimension">
        <strong>${escapeHtml(label)}</strong>
        <span class="tag ${runComparabilityStatusClass(status)}">${escapeHtml(comparabilityLabel(status))}</span>
        <span>${escapeHtml(comparabilityLabel(reason))}</span>
      </div>
    `;
  }).join("");
  const blockingReasons = Array.isArray(automaticRanking.blocking_reasons)
    ? automaticRanking.blocking_reasons.filter((item) => typeof item === "string")
    : [];
  const rankingEligible = automaticRanking.eligible === true;
  const rankingReason = typeof automaticRanking.reason === "string"
    ? comparabilityLabel(automaticRanking.reason)
    : "No ranking decision was reported.";
  const methodIdentity = typeof environment.method_identity_included === "boolean"
    ? environment.method_identity_included ? "Included" : "Excluded"
    : "Not reported";
  const attestation = report.operator_attestation || {};
  const attestationStatus = typeof attestation.status === "string"
    ? comparabilityLabel(attestation.status)
    : "Not reported";
  return `
    <section class="run-comparability-panel" aria-label="Run comparability">
      <div class="run-comparability-heading">
        <div>
          <h3>Comparability</h3>
          <p>Conservative contract identity and reproducibility evidence for this recorded Run.</p>
        </div>
        <span class="tag ${rankingEligible ? "status-ready" : "status-incomplete"}">${rankingEligible ? "Automatic ranking eligible" : "Automatic ranking unavailable"}</span>
      </div>
      <div class="run-comparability-fingerprints">
        <div>
          <span>Environment evaluation</span>
          ${runComparabilityFingerprint(environment.digest)}
          <small>Source: ${escapeHtml(comparabilityLabel(environment.source_granularity || "not_reported"))} · Strength: ${escapeHtml(comparabilityLabel(environment.comparison_strength || "not_reported"))} · Method identity: ${escapeHtml(methodIdentity)}</small>
        </div>
        <div>
          <span>Objective</span>
          ${runComparabilityFingerprint(objective.digest)}
          <small>Scope: ${escapeHtml(comparabilityLabel(objective.scope || "not_reported"))}</small>
        </div>
      </div>
      <div class="run-comparability-dimensions" aria-label="Reproducibility dimensions">
        ${dimensionRows}
      </div>
      <div class="run-comparability-ranking ${rankingEligible ? "eligible" : "blocked"}">
        <strong>${rankingEligible ? "Automatic cross-run ranking is eligible." : "Why automatic cross-run ranking is unavailable"}</strong>
        <span>${escapeHtml(rankingReason)}</span>
        ${blockingReasons.length ? `<span>Blocking evidence: ${escapeHtml(blockingReasons.map(comparabilityLabel).join(", "))}.</span>` : ""}
      </div>
      <p class="run-comparability-note">Operator attestation: ${escapeHtml(attestationStatus)}. Matching fingerprints alone do not establish reproducible comparability, and attestation does not upgrade dimension status.</p>
    </section>
  `;
}

function runComparabilityFingerprint(digest) {
  if (typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) {
    return `<strong class="run-comparability-fingerprint unavailable">Not identified</strong>`;
  }
  return `<code class="run-comparability-fingerprint" title="${escapeHtml(digest)}">${escapeHtml(digest.slice(0, 16))}…</code>`;
}

function runComparabilityStatusClass(status) {
  if (status === "verified") return "status-ready";
  if (status === "unavailable") return "status-failed";
  return "status-incomplete";
}

function comparabilityLabel(value) {
  const label = String(value || "not reported").replace(/_/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function runProviderCapabilityNote(workbench) {
  const actions = workbench && workbench.capabilities && workbench.capabilities.actions || [];
  const unavailable = actions.filter((action) => !action.supported || !action.eligible);
  if (!unavailable.length) return "";
  const labels = unavailable.map((action) => `${capabilityActionLabel(action.action)} (${capabilityReason(action.reason)})`);
  return `<p class="run-capability-note"><strong>Candidate actions not available:</strong> ${escapeHtml(labels.join(", "))}.</p>`;
}

function capabilityActionLabel(action, workspaceId = "") {
  if (action === "inspect") return "View details";
  if (action === "keep_editable") return workspaceId ? "Open Workspace" : "Edit in Workspace";
  if (action === "open_read_only") return "View files";
  if (action === "debug_run") return "Run headless";
  if (action === "environment_preview") return "Open interactive interface";
  if (action === "evaluate_child_run") return "Re-evaluate in a new Run";
  return String(action || "action")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function capabilityReason(reason) {
  const code = String(reason || "not_eligible");
  const messages = {
    candidate_derivation_unavailable: "This Run did not save enough information to open or try this Candidate.",
    candidate_content_unavailable: "The saved files or values for this Candidate are unavailable.",
    evaluation_content_unavailable: "The Environment setup saved with this Candidate is unavailable.",
    operator_job_provider_unavailable: "This OptPilot installation cannot try Candidates.",
    debug_run_provider_mismatch: "The software setup saved with this Run is not available now. Start a new Run to use the current setup.",
    debug_run_compiler_unsupported: "This Candidate cannot be tried with the current OptPilot installation.",
    debug_run_candidate_input_invalid: "The saved Candidate inputs are incomplete or invalid.",
    debug_run_runtime_kind_unsupported: "This Candidate cannot be tried with the current OptPilot installation.",
    debug_run_runtime_portability_unsupported: "This Candidate cannot be tried with the current OptPilot installation.",
    debug_run_prepared_layers_unsupported: "This Candidate cannot be tried with the current OptPilot installation.",
    debug_run_source_layers_unsupported: "This Candidate cannot be tried with the current OptPilot installation.",
    environment_preview_provider_unavailable: "This OptPilot installation cannot open interactive Candidate views.",
    environment_preview_profile_unavailable: "This Run's saved Environment version does not include an interactive interface.",
    environment_preview_profile_incompatible: "This Environment's interactive interface cannot run in the current OptPilot installation.",
    environment_preview_image_untrusted: "The interactive interface requires software that this OptPilot installation has not approved.",
  };
  return messages[code] || "This action is unavailable for this Candidate.";
}

function renderWorkbenchPage(detail, kind) {
  const labels = Object.fromEntries(runWorkbenchTabs());
  const page = workbenchPage(detail, kind);
  const items = Array.isArray(page.items) ? page.items : [];
  const entityCapability = detail.workbench.capabilities && detail.workbench.capabilities.entity_pages || {};
  if (!entityCapability.supported || !entityCapability.eligible) {
    return emptyInline(`${labels[kind] || capabilityActionLabel(kind)} unavailable: ${capabilityReason(entityCapability.reason)}`);
  }
  if (kind === "candidate") return renderCandidateResultsPage(detail, page);
  if (kind === "observation") return renderIndividualObservationsPage(detail, page);
  const paging = page.page || {};
  return `
    <div class="workbench-page-heading">
      <div>
        <h3>${escapeHtml(labels[kind] || capabilityActionLabel(kind))}</h3>
        <p>Showing records from this Run.</p>
      </div>
      <span class="tag">${escapeHtml(items.length)} shown</span>
    </div>
    <div class="workbench-entity-list">
      ${items.map((item) => renderWorkbenchItem(item, page)).join("") || emptyInline(`No ${String(labels[kind] || kind).toLowerCase()} are available for this Run.`)}
    </div>
    ${paging.has_more ? `<button class="ghost-button run-page-more" data-run-page-more="${escapeHtml(kind)}" type="button" ${state.runPageLoadingKind === kind ? "disabled" : ""}>${state.runPageLoadingKind === kind ? "Loading…" : "Load more"}</button>` : ""}
  `;
}

function routedCandidateResolution(detail = state.selectedRun) {
  const resolution = detail && detail.candidate_resolution;
  if (
    !state.routedCandidateId
    || !resolution
    || resolution.schema !== "optpilot.run-candidate-resolution.v1"
    || resolution.run_id !== selectedCanonicalRunId()
    || resolution.candidate_id !== state.routedCandidateId
  ) return null;
  return resolution;
}

function renderRoutedCandidateNotice(detail) {
  const resolution = routedCandidateResolution(detail);
  if (!state.routedCandidateId) return "";
  const candidateAlreadyLoaded = (workbenchPage(detail, "candidate").items || [])
    .some((item) => item && item.id === state.routedCandidateId);
  if (!resolution && candidateAlreadyLoaded) return "";
  if (!resolution) {
    return `
      <section class="candidate-route-notice error" role="alert">
        <div><strong>Candidate could not be opened</strong><span>The Run did not return a durable Candidate resolution. Refresh or return to all Candidates.</span></div>
        <button class="ghost-button compact-action" data-clear-candidate-route type="button">All Candidates</button>
      </section>
    `;
  }
  const status = String(resolution.status || "not_found");
  const card = resolution.shortlist_card && typeof resolution.shortlist_card === "object"
    ? resolution.shortlist_card
    : null;
  const note = card && typeof card.note === "string" ? card.note : "";
  const className = status === "not_found" ? "error" : status === "retired" || status === "saved_only" ? "warning" : "ready";
  const title = status === "not_found"
    ? "Candidate not found"
    : status === "retired"
    ? "Candidate from an unavailable Run"
    : status === "saved_only"
    ? "Saved Candidate snapshot"
    : `Focused Candidate ${resolution.candidate_id}`;
  return `
    <section class="candidate-route-notice ${className}" role="${status === "not_found" ? "alert" : "status"}">
      <div>
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(resolution.message || "Candidate resolution is unavailable.")}</span>
        ${note ? `<small>Shortlist note: ${escapeHtml(note)}</small>` : ""}
      </div>
      <div class="candidate-route-actions">
        ${card ? `<button class="ghost-button compact-action" data-open-candidate-shortlist type="button">Open Shortlist</button>` : ""}
        <button class="ghost-button compact-action" data-clear-candidate-route type="button">All Candidates</button>
      </div>
    </section>
  `;
}

function renderCandidateResultsPage(detail, page) {
  const items = Array.isArray(page.items) ? page.items : [];
  const resolution = routedCandidateResolution(detail);
  const focusedCandidate = resolution && resolution.candidate && typeof resolution.candidate === "object"
    ? resolution.candidate
    : null;
  if (state.routedCandidateId) {
    return renderFocusedCandidatePage(detail, page, focusedCandidate);
  }
  const focusedAlreadyLoaded = Boolean(
    focusedCandidate && items.some((item) => item && item.id === focusedCandidate.id),
  );
  const visibleItems = focusedCandidate && !focusedAlreadyLoaded
    ? [focusedCandidate, ...items]
    : items;
  const paging = page.page || {};
  const resultCapability = page.capabilities && page.capabilities.candidate_results || {};
  const resultSummary = page.result_summary || page.candidate_result_summary || {};
  const rankingContext = candidateRankingContext(page);
  const firstResult = items[0] && items[0].data && items[0].data.result || {};
  const objective = resultSummary.objective || firstResult.objective || runObjective(detail.workbench.summary || detail.run);
  const aggregation = candidateAggregationLabel(objective.aggregation_mode);
  const objectiveSummary = objective.metric
    ? `Primary objective: ${objective.metric}${objective.direction ? ` · ${objective.direction}` : ""}${aggregation ? ` · ${aggregation}` : ""}. OptPilot compares Candidates only when they used the same seeds and repetitions.`
    : "Candidate objective results recorded by this Run.";
  const rankingDescription = rankingContext.multipleComparisonGroups
    ? rankingContext.rankingSupported && rankingContext.planScoped && rankingContext.rankingEligible
      ? " Ranks are shown within matching trial groups; there is no overall rank."
      : " Results used different trial settings, so there is no overall rank."
    : "";
  const objectiveDescription = `${objectiveSummary}${rankingDescription}`;
  const unavailable = resultCapability.supported === false || resultCapability.eligible === false;
  const emptyMessage = workbenchRunIsTerminal(detail)
    ? "Run ended before any candidates were accepted."
    : "Waiting for the Method to submit its first Candidate.";
  return `
    <div class="workbench-page-heading candidate-results-heading">
      <div>
        <h3>Candidate results</h3>
        <p>${escapeHtml(objectiveDescription)}</p>
      </div>
      <span class="tag">${escapeHtml(items.length)} shown${focusedCandidate && !focusedAlreadyLoaded ? " + focused Candidate" : ""}</span>
    </div>
    ${renderRoutedCandidateNotice(detail)}
    ${unavailable ? `<p class="candidate-results-unavailable">Candidate summaries are temporarily unavailable. Recorded trial results are still available under Run details.</p>` : `
      ${renderCandidateComparisonPanel()}
      <div class="workbench-entity-list candidate-results-list">
        ${visibleItems.map((item) => renderCandidateResultItem(item, page)).join("") || emptyInline(emptyMessage)}
      </div>
      ${paging.has_more ? `<button class="ghost-button run-page-more" data-run-page-more="candidate" type="button" ${state.runPageLoadingKind === "candidate" ? "disabled" : ""}>${state.runPageLoadingKind === "candidate" ? "Loading…" : "Load more"}</button>` : ""}
    `}
  `;
}

function renderFocusedCandidatePage(detail, page, candidate) {
  if (!candidate) {
    return `
      <div class="candidate-focused-heading">
        <button class="ghost-button compact-action" data-clear-candidate-route type="button">Back to Candidates</button>
      </div>
      ${renderRoutedCandidateNotice(detail)}
    `;
  }
  const data = candidate.data && typeof candidate.data === "object" ? candidate.data : {};
  const result = data.result && typeof data.result === "object" ? data.result : {};
  const counts = result.counts && typeof result.counts === "object" ? result.counts : {};
  const aggregate = result.aggregate && typeof result.aggregate === "object" ? result.aggregate : null;
  const objective = result.objective && typeof result.objective === "object" ? result.objective : {};
  const comparison = result.comparison && typeof result.comparison === "object" ? result.comparison : {};
  const status = candidateResultStatusPresentation(result);
  const selection = candidate.selection && typeof candidate.selection === "object" ? candidate.selection : {};
  const selectionId = String(selection.selection_id || "");
  const inspection = state.semanticInspections[selectionId];
  const inspectCapability = actionCapability(candidate, page, "inspect");
  const inspectKey = workbenchActionKey(selectionId, "inspect");
  const inspectPending = state.pendingWorkbenchActions.has(inspectKey);
  const inspectError = state.workbenchActionErrors[inspectKey];
  const objectiveLabel = objective.metric
    ? `${candidateAggregationLabel(objective.aggregation_mode) || "Aggregate"} ${objective.metric}`
    : "Objective";
  const coverage = `${counts.usable_objectives ?? 0}/${counts.logical_trials ?? 0}`;
  const ranking = comparison.eligible && Number.isInteger(comparison.rank)
    ? `#${comparison.rank}`
    : aggregate
    ? "Not ranked"
    : "Not available";
  return `
    <div class="candidate-focused-page" data-focused-candidate="${escapeHtml(candidate.id || "")}">
      <div class="candidate-focused-heading">
        <button class="ghost-button compact-action" data-clear-candidate-route type="button">Back to Candidates</button>
        <span class="status-pill ${status.className}">${escapeHtml(status.label)}</span>
      </div>
      <div class="candidate-focused-title">
        <div>
          <span class="eyebrow">Candidate from this Run</span>
          <h3 title="${escapeHtml(candidate.id || "")}">${escapeHtml(candidate.id || "Candidate")}</h3>
          <p>Review this Candidate and its results, then try it, save it to the Shortlist, or edit it in a Workspace.</p>
        </div>
      </div>
      <section class="candidate-focused-summary" aria-label="Candidate summary">
        <h4>Results from this Run</h4>
        <dl class="workbench-data-grid candidate-result-evidence">
          <div><dt>${escapeHtml(objectiveLabel)}</dt><dd>${aggregate ? formatMetric(aggregate.value) : "Not available"}</dd></div>
          <div><dt>Rank</dt><dd>${escapeHtml(ranking)}</dd></div>
          <div><dt>Trials with a usable objective</dt><dd>${escapeHtml(coverage)}</dd></div>
          <div><dt>Trial outcomes</dt><dd>${escapeHtml(counts.successful ?? 0)} successful · ${escapeHtml(counts.terminal_failures ?? 0)} failed</dd></div>
        </dl>
        ${candidateResultReason(result.reason || result.comparison && result.comparison.reason) ? `<p class="candidate-result-reason">${escapeHtml(candidateResultReason(result.reason || result.comparison && result.comparison.reason))}</p>` : ""}
      </section>
      ${renderFocusedCandidateActions(candidate, page)}
      ${renderCandidateComparisonPanel()}
      ${inspection
        ? renderCandidateInspection(inspection)
        : inspectPending
        ? `<div class="candidate-detail-loading" role="status">Loading saved values and evaluation details…</div>`
        : inspectError
        ? `<div class="selection-action-error" role="alert">Candidate details were unavailable: ${escapeHtml(inspectError)} <button class="ghost-button compact-action" data-retry-candidate-inspection="${escapeHtml(selectionId)}" type="button">Retry</button></div>`
        : inspectCapability.supported && !inspectCapability.eligible
        ? `<div class="candidate-detail-loading">Saved values are unavailable: ${escapeHtml(capabilityReason(inspectCapability.reason))}.</div>`
        : ""}
      ${operatorJobsSection(selectedCanonicalRunId(), String(candidate.id || ""))}
      ${renderFocusedCandidateMore(candidate, page)}
    </div>
  `;
}

function directCandidateTryMode(modes) {
  if (!Array.isArray(modes) || modes.length !== 1) return null;
  const mode = modes[0];
  if (!mode || !mode.eligible) return null;
  const profiles = Array.isArray(mode.profiles) ? mode.profiles : [];
  if (mode.action === "environment_preview" && profiles.length !== 1) return null;
  return mode;
}

function candidateTryPrimaryLabel(modes, pending) {
  if (pending) return "Starting…";
  if (!modes.length) return "Try unavailable";
  if (!modes.some((mode) => mode.eligible)) return "Why unavailable?";
  const directMode = directCandidateTryMode(modes);
  return directMode ? capabilityActionLabel(directMode.action) : "Try Candidate";
}

function candidateTrySubmitLabel(actionName) {
  if (actionName === "debug_run") return "Run headless";
  if (actionName === "environment_preview") return "Open interface";
  return "Start";
}

function renderFocusedCandidateActions(item, page) {
  const selection = item.selection && typeof item.selection === "object" ? item.selection : {};
  const selectionId = String(selection.selection_id || "");
  const debug = actionCapability(item, page, "debug_run");
  const preview = actionCapability(item, page, "environment_preview");
  const modes = [debug, preview].filter((action) => action.supported);
  const eligibleModes = modes.filter((action) => action.eligible);
  const contextual = ["open_read_only", "keep_editable"]
    .map((name) => actionCapability(item, page, name))
    .filter((action) => action.supported);
  const pending = ["debug_run", "environment_preview"].some((actionName) => (
    state.pendingWorkbenchActions.has(workbenchActionKey(selectionId, actionName))
  ));
  const availableContextual = contextual.filter((action) => action.eligible);
  const directMode = directCandidateTryMode(modes);
  const opensTryDialog = Boolean(modes.length) && !directMode;
  const primaryLabel = candidateTryPrimaryLabel(modes, pending);
  const availableFallback = availableContextual.some((action) => action.action === "open_read_only")
    && availableContextual.some((action) => action.action === "keep_editable")
    ? " You can still view files or edit in a Workspace."
    : availableContextual.some((action) => action.action === "open_read_only")
    ? " You can still view its saved files."
    : availableContextual.some((action) => action.action === "keep_editable")
    ? " You can still edit it in a Workspace."
    : "";
  const tryReason = eligibleModes.length
    ? "Try this Candidate in its Environment without changing the source Run or its results."
    : modes.length
    ? `No try option is available with this Run's saved setup. Open Why unavailable? to see each reason.${availableFallback}`
    : `This Candidate cannot be tried with this Run's saved setup.${availableFallback}`;
  const showTryStatus = pending || !eligibleModes.length;
  const failures = [...modes, ...contextual]
    .map((action) => ({
      action,
      error: state.workbenchActionErrors[workbenchActionKey(selectionId, action.action)],
    }))
    .filter((failure) => Boolean(failure.error));
  return `
    <section class="candidate-focused-actions" aria-labelledby="candidate-actions-title">
      <div class="candidate-focused-action-heading">
        <h4 id="candidate-actions-title">Candidate actions</h4>
        <p>Use this Candidate without changing the source Run or its recorded results.</p>
      </div>
      <div class="candidate-focused-action-toolbar">
        <div class="candidate-focused-primary-actions">
          <button class="primary-button" data-try-candidate="${escapeHtml(selectionId)}" type="button" ${opensTryDialog ? 'aria-haspopup="dialog" aria-controls="candidateTryDialog"' : ""} ${!selectionId || !modes.length || pending ? "disabled" : ""}>${escapeHtml(primaryLabel)}</button>
          ${contextual.map((action) => renderSelectionActionControl(selectionId, action)).join("")}
        </div>
        <div class="candidate-focused-secondary-actions">
          ${renderCandidateReviewAction(item)}
          ${renderCandidateComparisonAction(item, page)}
        </div>
      </div>
      ${state.candidateTryNotice ? `<p class="candidate-focused-action-help candidate-try-refresh-notice" data-candidate-try-notice role="status" tabindex="-1">${escapeHtml(state.candidateTryNotice)}</p>` : ""}
      ${showTryStatus ? `<p class="candidate-focused-action-help" data-candidate-try-status tabindex="-1" ${pending ? 'role="status"' : ""}>${escapeHtml(pending ? "Starting Candidate try…" : tryReason)}</p>` : ""}
      ${failures.map(({ action, error }) => `
        <div class="selection-action-error" role="alert" data-focused-action-error="${escapeHtml(action.action)}">
          <span>${escapeHtml(error)}</span>
          <span>${escapeHtml(["debug_run", "environment_preview"].includes(action.action)
            ? "Use Try Candidate to retry."
            : `Use ${capabilityActionLabel(action.action, action.workspace_id)} to retry.`)}</span>
        </div>
      `).join("")}
    </section>
  `;
}

function renderFocusedCandidateMore(item, page) {
  const data = item.data && typeof item.data === "object" ? item.data : {};
  const context = item.context && typeof item.context === "object" ? item.context : {};
  const environment = context.environment && typeof context.environment === "object" ? context.environment : {};
  const selection = item.selection && typeof item.selection === "object" ? item.selection : {};
  const selectionId = String(selection.selection_id || "");
  const reEvaluate = actionCapability(item, page, "evaluate_child_run");
  const pending = state.pendingWorkbenchActions.has(workbenchActionKey(selectionId, "evaluate_child_run"));
  return `
    <details class="candidate-focused-more">
      <summary>More actions and details</summary>
      <div class="candidate-focused-more-actions">
        ${reEvaluate.supported ? `<button class="ghost-button compact-action" data-workbench-action="evaluate_child_run" data-workbench-selection="${escapeHtml(selectionId)}" type="button" ${!reEvaluate.eligible || pending ? "disabled" : ""} title="${escapeHtml(reEvaluate.eligible ? "Re-evaluate in a new Run" : capabilityReason(reEvaluate.reason))}">${escapeHtml(pending ? "Starting…" : "Re-evaluate in a new Run")}</button>` : ""}
        ${selectionId ? `<button class="ghost-button compact-action" data-workbench-ask-assistant="${escapeHtml(selectionId)}" type="button">Ask in ${escapeHtml(assistantSessionLabel())}</button>` : ""}
      </div>
      ${reEvaluate.supported && !reEvaluate.eligible ? `<p>Re-evaluation is unavailable: ${escapeHtml(capabilityReason(reEvaluate.reason))}</p>` : ""}
      <dl class="workbench-data-grid candidate-context-details">
        <div><dt>Candidate format</dt><dd>${escapeHtml(data.format || "Not reported")}</dd></div>
        <div><dt>Environment</dt><dd>${escapeHtml(environment.id || "Not reported")}</dd></div>
        <div><dt>Environment version</dt><dd title="${escapeHtml(environment.revision || "")}">${escapeHtml(environment.revision ? shortDigest(environment.revision) : "Not reported")}</dd></div>
        <div><dt>Source Run</dt><dd>${escapeHtml(selectedCanonicalRunId() || "Unknown")}</dd></div>
      </dl>
      ${renderSelectionTechnicalDetails(selection, item)}
    </details>
  `;
}

function ensureFocusedCandidateInspection() {
  if (!state.routedCandidateId || state.activeRunTab !== "candidate") return;
  const item = currentWorkbenchItemForCandidate(state.routedCandidateId);
  if (!item || !item.selection || !item.selection.selection_id) return;
  const page = workbenchPage(state.selectedRun, "candidate");
  const capability = actionCapability(item, page, "inspect");
  const selectionId = item.selection.selection_id;
  const key = workbenchActionKey(selectionId, "inspect");
  if (
    !capability.eligible
    || state.semanticInspections[selectionId]
    || state.pendingWorkbenchActions.has(key)
    || state.workbenchActionErrors[key]
  ) return;
  performWorkbenchAction("inspect", selectionId);
}

function currentWorkbenchItemForCandidate(candidateId) {
  const page = workbenchPage(state.selectedRun, "candidate");
  const loaded = (page.items || []).find((item) => item && item.id === candidateId);
  if (loaded) return loaded;
  const resolution = routedCandidateResolution();
  if (resolution && resolution.candidate && resolution.candidate.id === candidateId) {
    return resolution.candidate;
  }
  return null;
}

function workbenchRunIsTerminal(detail) {
  const summary = detail && detail.workbench && detail.workbench.summary || detail && detail.run || {};
  return ["completed", "succeeded", "failed", "cancelled"].includes(runStatus(summary));
}

function candidateComparisonSelection(item, page) {
  const selection = item && item.selection && typeof item.selection === "object" ? item.selection : null;
  const capability = actionCapability(item, page, "compare");
  if (item && item.kind === "candidate" && capability.eligible && selection && selection.selection_id) {
    return {
      selection_id: String(selection.selection_id),
      candidate_id: String(item.id || selection.entity_id || ""),
      presentation_selection: { ...selection },
    };
  }
  return null;
}

function renderCandidateComparisonAction(item, page) {
  const candidate = candidateComparisonSelection(item, page);
  if (!candidate) return "";
  const baseline = state.candidateComparisonBaseline;
  const isBaseline = baseline && baseline.selection_id === candidate.selection_id;
  const pending = state.candidateComparisonLoading
    && state.candidateComparisonCandidate
    && state.candidateComparisonCandidate.selection_id === candidate.selection_id;
  const label = isBaseline
    ? "Comparison baseline"
    : pending
      ? "Comparing…"
      : baseline
        ? "Compare with baseline"
        : "Compare";
  const description = isBaseline
    ? "Choose Compare on another candidate."
    : baseline
      ? `Compare with ${baseline.candidate_id}.`
      : "Use this Candidate as the baseline.";
  return `
    <div class="candidate-compare-action">
      <button class="ghost-button compact-action" data-candidate-compare="${escapeHtml(candidate.selection_id)}" type="button" ${isBaseline || pending ? "disabled" : ""}>${escapeHtml(label)}</button>
      <span>${escapeHtml(description)}</span>
    </div>
  `;
}

function reviewCollection(detail = state.selectedRun) {
  const value = detail && detail.review_collection;
  return value && typeof value === "object" ? value : null;
}

function reviewContainsCandidate(candidateId, detail = state.selectedRun) {
  return Boolean(reviewCandidateCard(candidateId, detail));
}

function reviewCandidateCard(candidateId, detail = state.selectedRun) {
  const collection = reviewCollection(detail);
  const items = Array.isArray(collection && collection.items) ? collection.items : [];
  return items.find((item) => item && item.selection && item.selection.kind === "candidate" && item.selection.entity_id === candidateId) || null;
}

function renderCandidateReviewAction(item) {
  const selection = item && item.selection || {};
  const selectionId = String(selection.selection_id || "");
  if (!selectionId || item.kind !== "candidate") return "";
  const savedCard = reviewCandidateCard(item.id);
  const included = Boolean(savedCard);
  const savedSelection = savedCard && savedCard.selection || {};
  const savedSelectionDigest = String(savedSelection.selection_digest || "");
  const currentSelectionDigest = String(selection.selection_digest || "");
  const updateAvailable = included
    && Boolean(savedSelectionDigest)
    && Boolean(currentSelectionDigest)
    && savedSelectionDigest !== currentSelectionDigest;
  const pending = state.reviewPendingSelectionIds.has(selectionId);
  const savedAt = included ? formatRealmTime(savedCard.saved_result_at) : "";
  const buttonLabel = pending
    ? updateAvailable ? "Updating…" : "Saving…"
    : updateAvailable ? "Update saved result" : included ? "Saved to Shortlist" : "Save to Shortlist";
  const evidenceStatus = updateAvailable
    ? "newer exact result available"
    : "matches the current exact result";
  const error = state.reviewSelectionErrors[selectionId];
  return `
    <div class="candidate-review-action">
      <button class="${included && !updateAvailable ? "ghost-button" : "primary-button"} compact-action" data-add-to-review="${escapeHtml(selectionId)}" ${updateAvailable ? 'data-update-saved-result="true"' : ""} type="button" ${included && !updateAvailable || pending ? "disabled" : ""}>${escapeHtml(buttonLabel)}</button>
      <span>${included
        ? `Saved result: ${escapeHtml(savedAt || "time unavailable")} · ${escapeHtml(evidenceStatus)}.`
        : "Save this Candidate and its recorded result. This does not create a Workspace or run the Candidate."}</span>
      ${error ? `<div class="selection-action-error" role="alert">${escapeHtml(error)} <button class="ghost-button compact-action" data-add-to-review="${escapeHtml(selectionId)}" ${updateAvailable ? 'data-update-saved-result="true"' : ""} type="button">Try again</button></div>` : ""}
    </div>
  `;
}

function renderCandidateComparisonPanel() {
  const baseline = state.candidateComparisonBaseline;
  if (!baseline && !state.candidateComparisonError) return "";
  const candidate = state.candidateComparisonCandidate;
  const projection = state.candidateComparisonProjection;
  return `
    <section class="candidate-comparison-panel" aria-label="Candidate comparison">
      <div class="candidate-comparison-heading">
        <div>
          <span class="eyebrow">Candidate comparison</span>
          <h4>Candidates</h4>
          <p>${candidate ? "Recorded outcomes and Candidate inputs from the same Run update." : "Choose Compare on a second Candidate."}</p>
        </div>
        <div class="candidate-comparison-controls">
          ${candidate && projection ? `<button class="ghost-button compact-action candidate-comparison-swap" type="button" ${state.candidateComparisonLoading ? "disabled" : ""}>Swap</button>` : ""}
          <button class="ghost-button compact-action candidate-comparison-clear" type="button">Clear</button>
        </div>
      </div>
      <div class="candidate-comparison-operands">
        <div><span>Baseline</span><strong>${escapeHtml(baseline && baseline.candidate_id || "-")}</strong></div>
        <div><span>Comparison</span><strong>${escapeHtml(candidate && candidate.candidate_id || "Choose another candidate")}</strong></div>
      </div>
      ${state.candidateComparisonLoading ? `<div class="candidate-comparison-notice" role="status">Comparing recorded results…</div>` : ""}
      ${state.candidateComparisonError ? `<div class="candidate-comparison-notice error" role="alert">${escapeHtml(state.candidateComparisonError)}</div>` : ""}
      ${projection ? renderCandidateComparisonProjection(projection) : ""}
    </section>
  `;
}

function renderCandidateComparisonProjection(projection) {
  const eligibility = projection && projection.eligibility || {};
  const operands = Array.isArray(projection && projection.operands) ? projection.operands : [];
  const baseline = operands.find((operand) => operand && operand.role === "baseline") || {};
  const comparison = operands.find((operand) => operand && operand.role === "comparison") || {};
  if (!eligibility.supported || !eligibility.eligible) {
    return `<div class="candidate-comparison-notice warning">Comparison unavailable: ${escapeHtml(eligibility.reason || eligibility.code || "These Candidate types cannot be compared here.")}</div>`;
  }
  return `
    ${renderCandidateOutcomeComparison(projection && projection.outcomes, baseline, comparison)}
    ${renderCandidateInputComparison(projection && projection.candidate_input, baseline, comparison)}
  `;
}

function renderCandidateOutcomeComparison(outcomes, baseline, comparison) {
  const eligibility = outcomes && outcomes.eligibility || {};
  const evaluationPlan = outcomes && outcomes.evaluation_plan || {};
  const metrics = outcomes && outcomes.metrics || {};
  const rows = Array.isArray(metrics.rows) ? metrics.rows : [];
  const planBaseline = evaluationPlan.baseline || {};
  const planComparison = evaluationPlan.comparison || {};
  return `
    <section class="candidate-comparison-section candidate-outcome-comparison" aria-label="Outcome comparison">
      <div class="candidate-comparison-section-heading">
        <div>
          <span class="eyebrow">Outcomes</span>
          <h5>Recorded evaluation metrics</h5>
        </div>
        <span class="tag">${escapeHtml(eligibility.eligible ? "Available" : "Unavailable")}</span>
      </div>
      ${!eligibility.supported || !eligibility.eligible ? `
        <div class="candidate-comparison-notice warning">Outcome comparison unavailable: ${escapeHtml(eligibility.reason || eligibility.code || "This Run does not contain comparable results.")}</div>
      ` : `
        <div class="candidate-comparison-plan" data-plan-relation="${escapeHtml(evaluationPlan.relation || "unknown")}">
          <div><span>Evaluation plans</span><strong>${escapeHtml(capabilityActionLabel(evaluationPlan.relation || "unknown"))}</strong></div>
          <div><span>Baseline coordinates</span><strong>${escapeHtml(planBaseline.coordinate_count ?? "-")}</strong></div>
          <div><span>Comparison coordinates</span><strong>${escapeHtml(planComparison.coordinate_count ?? "-")}</strong></div>
        </div>
        <div class="candidate-comparison-summary">
          <div><span>Metrics authored</span><strong>${escapeHtml(metrics.total ?? 0)}</strong></div>
          <div><span>Metrics shown</span><strong>${escapeHtml(metrics.returned ?? 0)}</strong></div>
          <div><span>Metrics omitted</span><strong>${escapeHtml(metrics.omitted ?? 0)}</strong></div>
        </div>
        <div class="candidate-comparison-table-wrap">
          <table class="candidate-comparison-table candidate-outcome-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>${escapeHtml(baseline.candidate && baseline.candidate.id || "Baseline")}</th>
                <th>${escapeHtml(comparison.candidate && comparison.candidate.id || "Comparison")}</th>
                <th>Relation</th>
                <th>Delta</th>
                <th>Preferred</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((row) => renderCandidateOutcomeMetricRow(row, baseline, comparison)).join("") || `<tr><td colspan="6">No recorded metrics were returned.</td></tr>`}
            </tbody>
          </table>
        </div>
      `}
      ${renderCandidateConstraintComparison(outcomes && outcomes.constraints, baseline, comparison)}
    </section>
  `;
}

function renderCandidateOutcomeMetricRow(row, baseline, comparison) {
  const relation = row && row.relation || {};
  const metadata = [
    row && row.role,
    row && row.direction,
    candidateAggregationLabel(row && row.aggregation_mode),
  ].filter(Boolean).map(capabilityActionLabel).join(" · ");
  let preferred = "Not defined";
  if (relation.preferred_operand === "baseline") {
    preferred = baseline.candidate && baseline.candidate.id || "Baseline";
  } else if (relation.preferred_operand === "comparison") {
    preferred = comparison.candidate && comparison.candidate.id || "Comparison";
  } else if (relation.preferred_operand === "tie") {
    preferred = "Tie";
  }
  return `
    <tr data-outcome-relation="${escapeHtml(relation.numeric || relation.reason || "unavailable")}">
      <th scope="row"><strong>${escapeHtml(row && row.name || "Metric")}${row && row.name_truncated ? "…" : ""}</strong>${metadata ? `<small>${escapeHtml(metadata)}</small>` : ""}</th>
      <td>${renderCandidateOutcomeMetricCell(row && row.baseline)}</td>
      <td>${renderCandidateOutcomeMetricCell(row && row.comparison)}</td>
      <td>${relation.eligible ? escapeHtml(capabilityActionLabel(relation.numeric || "comparable")) : `<span class="candidate-comparison-hidden">${escapeHtml(relation.reason || "Unavailable")}</span>`}</td>
      <td>${relation.delta === null || relation.delta === undefined ? `<span class="candidate-comparison-missing">Not available</span>` : `<strong>${formatCell(relation.delta)}</strong><small class="candidate-comparison-cell-note">${escapeHtml(relation.delta_semantics || "")}</small>`}</td>
      <td>${escapeHtml(preferred)}</td>
    </tr>
  `;
}

function renderCandidateOutcomeMetricCell(cell) {
  const aggregate = cell && cell.aggregate;
  const coverage = cell && cell.coverage || {};
  return `
    <div class="candidate-outcome-cell">
      ${aggregate ? `<strong>${formatCell(aggregate.value)}</strong><span>${escapeHtml(aggregate.sample_count ?? 0)} samples</span>` : `<strong class="candidate-comparison-missing">Incomplete</strong><span>${escapeHtml(cell && cell.reason || "No combined result available")}</span>`}
      <small>Coverage: ${escapeHtml(coverage.usable ?? 0)} usable · ${escapeHtml(coverage.successful ?? 0)} successful · ${escapeHtml(coverage.terminal ?? 0)} terminal · ${escapeHtml(coverage.active ?? 0)} active · ${escapeHtml(coverage.planned ?? 0)} planned</small>
    </div>
  `;
}

function renderCandidateConstraintComparison(constraints, baseline, comparison) {
  const eligibility = constraints && constraints.eligibility || {};
  const rows = constraints && Array.isArray(constraints.rows) ? constraints.rows : [];
  const baselineLabel = baseline && baseline.candidate && baseline.candidate.id || "Baseline";
  const comparisonLabel = comparison && comparison.candidate && comparison.candidate.id || "Comparison";
  return `
    <div class="candidate-constraint-comparison">
      <div class="candidate-comparison-section-heading compact">
        <div><span class="eyebrow">Constraints</span><h5>Evaluator constraints</h5></div>
        <span class="tag">${escapeHtml(eligibility.eligible ? "Available" : "Unavailable")}</span>
      </div>
      ${eligibility.supported && eligibility.eligible
        ? `<div class="candidate-comparison-table-wrap"><table class="candidate-comparison-table candidate-constraint-table"><thead><tr><th>Constraint</th><th>${escapeHtml(baselineLabel)}</th><th>${escapeHtml(comparisonLabel)}</th><th>Relation</th><th>Preferred</th></tr></thead><tbody>${rows.map((row) => renderCandidateConstraintRow(row, baselineLabel, comparisonLabel)).join("") || `<tr><td colspan="5">No boolean constraints were returned.</td></tr>`}</tbody></table></div>`
        : `<div class="candidate-comparison-notice warning">Constraint comparison unavailable: ${escapeHtml(eligibility.reason || eligibility.code || "This Run does not contain comparable constraints.")}</div>`}
    </div>
  `;
}

function renderCandidateConstraintRow(row, baselineLabel, comparisonLabel) {
  const relation = row && row.relation || {};
  let preferred = "No preference";
  if (relation.preferred_operand === "baseline") preferred = baselineLabel;
  else if (relation.preferred_operand === "comparison") preferred = comparisonLabel;
  else if (relation.preferred_operand === "tie") preferred = "Tie";
  return `<tr><th scope="row">${escapeHtml(row && row.name || "Constraint")}${row && row.name_truncated ? "…" : ""}<small>Boolean satisfied / violated</small></th><td>${renderCandidateConstraintCell(row && row.baseline)}</td><td>${renderCandidateConstraintCell(row && row.comparison)}</td><td>${relation.eligible ? escapeHtml(capabilityActionLabel(relation.relation || "comparable")) : `<span class="candidate-comparison-hidden">${escapeHtml(relation.reason || "Unavailable")}</span>`}</td><td>${escapeHtml(preferred)}</td></tr>`;
}

function renderCandidateConstraintCell(cell) {
  const coverage = cell && cell.coverage || {};
  const label = cell && cell.status === "complete"
    ? cell.all_satisfied ? "Satisfied" : "Violated"
    : "Incomplete";
  return `<div class="candidate-outcome-cell"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(coverage.satisfied ?? 0)} satisfied · ${escapeHtml(coverage.violated ?? 0)} violated</span><small>${escapeHtml(coverage.successful ?? 0)} successful · ${escapeHtml(coverage.planned ?? 0)} planned${cell && cell.reason ? ` · ${escapeHtml(cell.reason)}` : ""}</small></div>`;
}

function renderCandidateInputComparison(candidateInput, baseline, comparison) {
  const eligibility = candidateInput && candidateInput.eligibility || {};
  const summary = candidateInput && candidateInput.summary || {};
  const parameters = candidateInput && candidateInput.parameters;
  const rows = parameters && Array.isArray(parameters.rows) ? parameters.rows : [];
  const files = candidateInput && candidateInput.files;
  const fileRows = files && Array.isArray(files.rows) ? files.rows : [];
  const metadata = candidateInput && candidateInput.metadata;
  const metadataRows = metadata && Array.isArray(metadata.rows) ? metadata.rows : [];
  const format = candidateInput && candidateInput.format || "candidate";
  return `
    <section class="candidate-comparison-section candidate-input-comparison" aria-label="Candidate input comparison">
      <div class="candidate-comparison-section-heading">
        <div>
          <span class="eyebrow">Candidate input</span>
          <h5>${escapeHtml(capabilityActionLabel(format))}</h5>
        </div>
        <span class="tag">${escapeHtml(eligibility.eligible ? "Available" : "Unavailable")}</span>
      </div>
      ${!eligibility.supported || !eligibility.eligible ? `
        <div class="candidate-comparison-notice warning">Candidate input comparison unavailable: ${escapeHtml(eligibility.reason || eligibility.code || "This Candidate format cannot be compared here.")}</div>
      ` : `
    <div class="candidate-comparison-summary">
      <div><span>${format === "files" ? "Files" : "Fields"}</span><strong>${escapeHtml(summary.rows ?? 0)}</strong></div>
      <div><span>Same</span><strong>${escapeHtml(summary.same ?? 0)}</strong></div>
      <div><span>Changed</span><strong>${escapeHtml(summary.changed ?? 0)}</strong></div>
      <div><span>Added</span><strong>${escapeHtml(summary.added ?? 0)}</strong></div>
      <div><span>Removed</span><strong>${escapeHtml(summary.removed ?? 0)}</strong></div>
      ${format !== "files" ? `<div><span>Hidden values</span><strong>${escapeHtml(summary.hidden ?? 0)}</strong></div>` : ""}
    </div>
    ${files
      ? renderCandidateFileInputComparison(files, fileRows, baseline, comparison)
      : metadata
        ? renderCandidateMetadataInputComparison(metadataRows, baseline, comparison)
        : `
    <div class="candidate-comparison-table-wrap">
      <table class="candidate-comparison-table">
        <thead>
          <tr>
            <th>Parameter</th>
            <th>${escapeHtml(baseline.candidate && baseline.candidate.id || "Baseline")}</th>
            <th>${escapeHtml(comparison.candidate && comparison.candidate.id || "Comparison")}</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(renderCandidateComparisonRow).join("") || `<tr><td colspan="4">No parameter differences were returned.</td></tr>`}
        </tbody>
      </table>
    </div>
    `}
      `}
    </section>
  `;
}

function renderCandidateMetadataInputComparison(rows, baseline, comparison) {
  const baselineLabel = baseline && baseline.candidate && baseline.candidate.id || "Baseline";
  const comparisonLabel = comparison && comparison.candidate && comparison.candidate.id || "Comparison";
  return `
    <p class="candidate-comparison-notice">This comparison shows only safe top-level metadata. Opaque values are not interpreted.</p>
    <div class="candidate-comparison-table-wrap">
      <table class="candidate-comparison-table candidate-metadata-comparison-table">
        <thead><tr><th>Metadata field</th><th>${escapeHtml(baselineLabel)}</th><th>${escapeHtml(comparisonLabel)}</th><th>Change</th></tr></thead>
        <tbody>${rows.map(renderCandidateMetadataComparisonRow).join("") || `<tr><td colspan="4">No top-level metadata fields were returned.</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

function renderCandidateMetadataComparisonRow(row) {
  const name = row && row.name_redacted ? "Hidden field" : row && row.name || "metadata";
  return `<tr data-comparison-change="${escapeHtml(row && row.change || "same")}"><th scope="row"><strong>${escapeHtml(name)}</strong></th><td>${renderCandidateComparisonCell(row && row.baseline)}</td><td>${renderCandidateComparisonCell(row && row.comparison)}</td><td><span class="tag comparison-change-${escapeHtml(row && row.change || "same")}">${escapeHtml(capabilityActionLabel(row && row.change || "same"))}</span></td></tr>`;
}

function renderCandidateFileInputComparison(files, rows, baseline, comparison) {
  const entrypoint = files && files.entrypoint || {};
  const directories = files && files.directories || {};
  const baselineLabel = baseline && baseline.candidate && baseline.candidate.id || "Baseline";
  const comparisonLabel = comparison && comparison.candidate && comparison.candidate.id || "Comparison";
  return `
    <div class="candidate-file-comparison-facts">
      <div><span>Entrypoint</span><strong>${escapeHtml(entrypoint.change === "same" ? entrypoint.baseline || "Not set" : "Changed")}</strong><small>${escapeHtml(entrypoint.change === "same" ? "Same entrypoint" : `${entrypoint.baseline || "not set"} → ${entrypoint.comparison || "not set"}`)}</small></div>
      <div><span>Directories</span><strong>${escapeHtml(directories.same ? "Same" : "Changed")}</strong><small>${escapeHtml(directories.baseline_count ?? 0)} → ${escapeHtml(directories.comparison_count ?? 0)} declared directories</small></div>
      <div><span>Candidate options</span><strong>${files.options_equal ? "Same" : "Changed"}</strong><small>Values remain sealed and are not rendered as file semantics.</small></div>
    </div>
    <div class="candidate-comparison-table-wrap">
      <table class="candidate-comparison-table candidate-file-comparison-table">
        <thead><tr><th>Relative file</th><th>${escapeHtml(baselineLabel)}</th><th>${escapeHtml(comparisonLabel)}</th><th>Change</th></tr></thead>
        <tbody>${rows.map(renderCandidateFileComparisonRow).join("") || `<tr><td colspan="4">Both Candidate file lists are empty.</td></tr>`}</tbody>
      </table>
    </div>
    ${renderCandidateFileTextDiff(files && files.text_diff)}
  `;
}

function renderCandidateFileComparisonRow(row) {
  const detail = row && row.change === "changed"
    ? [row.content_equal === false ? "content" : "", row.executable_equal === false ? "mode" : ""].filter(Boolean).join(" and ")
    : "";
  const canDiff = row && row.change !== "same" && (
    row.baseline && row.baseline.present || row.comparison && row.comparison.present
  );
  return `<tr data-comparison-change="${escapeHtml(row && row.change || "same")}"><th scope="row"><strong>${escapeHtml(row && row.path || "file")}</strong>${detail ? `<small>Changed ${escapeHtml(detail)}</small>` : ""}</th><td>${renderCandidateFileComparisonCell(row && row.baseline)}</td><td>${renderCandidateFileComparisonCell(row && row.comparison)}</td><td><span class="tag comparison-change-${escapeHtml(row && row.change || "same")}">${escapeHtml(capabilityActionLabel(row && row.change || "same"))}</span>${canDiff ? `<button class="ghost-button compact-action candidate-file-text-diff-action" data-candidate-text-diff="${escapeHtml(row.path)}" type="button" ${state.candidateComparisonLoading ? "disabled" : ""}>View text diff</button>` : ""}</td></tr>`;
}

function renderCandidateFileTextDiff(textDiff) {
  if (!textDiff || textDiff.schema !== "optpilot.candidate-file-text-diff.v1") return "";
  const eligibility = textDiff.eligibility || {};
  const diff = textDiff.diff || {};
  return `
    <section class="candidate-file-text-diff" aria-label="Candidate file text diff">
      <div class="candidate-comparison-section-heading compact">
        <div><span class="eyebrow">Text diff</span><h5>${escapeHtml(textDiff.relative_path || "Selected file")}</h5></div>
        <span class="tag">${escapeHtml(eligibility.eligible ? "Available" : "Unavailable")}</span>
      </div>
      ${eligibility.eligible
        ? `<p class="candidate-comparison-notice">Text differences read directly from both Candidate files. No Workspace was created.</p><pre class="candidate-file-text-diff-body"><code>${escapeHtml(diff.text || "No textual line changes; file mode or final-newline metadata may differ.")}</code></pre><small>${escapeHtml(diff.line_count ?? 0)} diff lines · ${escapeHtml(formatBytes(diff.encoded_bytes ?? 0))} · complete</small>`
        : `<div class="candidate-comparison-notice warning">Text diff unavailable: ${escapeHtml(eligibility.reason || eligibility.code || "These files cannot be shown as UTF-8 text.")}</div>`}
    </section>
  `;
}

function renderCandidateFileComparisonCell(cell) {
  if (!cell || !cell.present) return `<span class="candidate-comparison-missing">Not present</span>`;
  return `<div class="candidate-file-cell"><strong>${escapeHtml(formatBytes(cell.size_bytes ?? 0))}</strong><small>${cell.executable ? "Executable" : "Regular file"}</small></div>`;
}

function renderCandidateComparisonRow(row) {
  const definition = row && row.definition || {};
  const parameterName = row && row.name_redacted
    ? "Hidden parameter"
    : row && row.name || "parameter";
  const metadata = [
    definition.value_type,
    definition.unit,
    definition.description,
  ].filter(Boolean).join(" · ");
  return `
    <tr data-comparison-change="${escapeHtml(row && row.change || "same")}">
      <th scope="row"><strong>${escapeHtml(parameterName)}</strong>${metadata ? `<small>${escapeHtml(metadata)}</small>` : ""}</th>
      <td>${renderCandidateComparisonCell(row && row.baseline)}</td>
      <td>${renderCandidateComparisonCell(row && row.comparison)}</td>
      <td><span class="tag comparison-change-${escapeHtml(row && row.change || "same")}">${escapeHtml(capabilityActionLabel(row && row.change || "same"))}</span></td>
    </tr>
  `;
}

function renderCandidateComparisonCell(cell) {
  if (!cell || !cell.present) return `<span class="candidate-comparison-missing">Not set</span>`;
  if (!cell.included) {
    return `<span class="candidate-comparison-hidden" title="${escapeHtml(cell.reason || "Value preview unavailable")}">Value hidden</span>`;
  }
  return formatCell(cell.value);
}

function clearCandidateComparison() {
  state.candidateComparisonRequestSeq += 1;
  state.candidateComparisonBaseline = null;
  state.candidateComparisonCandidate = null;
  state.candidateComparisonProjection = null;
  state.candidateComparisonLoading = false;
  state.candidateComparisonError = "";
  renderRunDetail();
}

function chooseCandidateComparison(selectionId) {
  const item = currentWorkbenchItem(selectionId);
  const page = item ? workbenchPage(state.selectedRun, item.kind) : null;
  const candidate = candidateComparisonSelection(item, page);
  if (!candidate || state.candidateComparisonLoading) return;
  const baseline = state.candidateComparisonBaseline;
  if (!baseline) {
    state.candidateComparisonRequestSeq += 1;
    state.candidateComparisonBaseline = candidate;
    state.candidateComparisonCandidate = null;
    state.candidateComparisonProjection = null;
    state.candidateComparisonError = "";
    renderRunDetail();
    return;
  }
  if (baseline.selection_id === candidate.selection_id) return;
  requestCandidateComparison(baseline, candidate);
}

async function requestCandidateComparison(baseline, candidate, options = {}) {
  const runId = selectedCanonicalRunId();
  const head = state.selectedRun && state.selectedRun.workbench && state.selectedRun.workbench.head;
  if (!runId || !head || !baseline || !candidate || baseline.selection_id === candidate.selection_id) return;
  const textDiffPath = options.textDiffPath
    ? safeSelectionRelativePath(options.textDiffPath)
    : null;
  if (options.textDiffPath && !textDiffPath) return;
  const requestSeq = ++state.candidateComparisonRequestSeq;
  state.candidateComparisonBaseline = baseline;
  state.candidateComparisonCandidate = candidate;
  if (!textDiffPath) state.candidateComparisonProjection = null;
  state.candidateComparisonLoading = true;
  state.candidateComparisonError = "";
  renderRunDetail();
  try {
    const projection = await postJson(`/api/runs/${encodeURIComponent(runId)}/candidate-comparison`, {
      schema: "optpilot.run-candidate-comparison-request.v2",
      baseline_selection: baseline.presentation_selection,
      comparison_selection: candidate.presentation_selection,
      text_diff_path: textDiffPath,
    });
    if (
      requestSeq !== state.candidateComparisonRequestSeq
      || selectedCanonicalRunId() !== runId
      || !sameRunHead(state.selectedRun && state.selectedRun.workbench && state.selectedRun.workbench.head, head)
    ) return;
    if (
      !projection
      || projection.schema !== "optpilot.run-candidate-comparison.v3"
      || projection.run_id !== runId
      || !sameRunHead(projection.head, head)
    ) throw new Error("Candidate comparison response differs from the selected Run update.");
    state.candidateComparisonProjection = projection;
  } catch (error) {
    if (requestSeq === state.candidateComparisonRequestSeq) {
      state.candidateComparisonError = boundedPublicActionError(
        error,
        "The Candidates could not be compared at this Run update.",
      );
    }
  } finally {
    if (requestSeq === state.candidateComparisonRequestSeq) {
      state.candidateComparisonLoading = false;
      renderRunDetail();
    }
  }
}

function requestCandidateFileTextDiff(relativePath) {
  const baseline = state.candidateComparisonBaseline;
  const candidate = state.candidateComparisonCandidate;
  if (!baseline || !candidate || state.candidateComparisonLoading) return;
  requestCandidateComparison(baseline, candidate, { textDiffPath: relativePath });
}

function swapCandidateComparison() {
  const baseline = state.candidateComparisonBaseline;
  const candidate = state.candidateComparisonCandidate;
  if (!baseline || !candidate || !state.candidateComparisonProjection || state.candidateComparisonLoading) return;
  requestCandidateComparison(candidate, baseline);
}

function candidateAggregationLabel(mode) {
  const labels = {
    mean: "Mean",
    median: "Median",
    min: "Minimum",
    max: "Maximum",
    sum: "Sum",
    last: "Last",
    weighted_mean: "Weighted mean",
  };
  return labels[String(mode || "")] || (mode ? capabilityActionLabel(mode) : "");
}

function candidateResultStatusPresentation(result) {
  const counts = result && result.counts || {};
  const reason = String(result && result.reason || "");
  if (result && (result.status === "rankable" || result.status === "aggregate_only")) {
    return { label: "Complete", className: "status-ready" };
  }
  if (reason === "objective_aggregation_not_supported") {
    return { label: "Not comparable", className: "status-incomplete" };
  }
  if (reason === "terminal_result_not_successful") {
    return { label: "Incomplete", className: "status-incomplete" };
  }
  if ([
    "terminal_observation_missing",
    "terminal_observation_not_successful",
    "primary_objective_missing_or_nonfinite",
    "aggregate_not_finite",
    "not_evaluated",
  ].includes(reason)) {
    return { label: "No usable result", className: "status-failed" };
  }
  if (reason === "candidate_evaluation_active") {
    return { label: "Evaluating", className: "status-running" };
  }
  if (Number(counts.terminal_failures || 0) > 0) {
    return { label: "Incomplete", className: "status-incomplete" };
  }
  if (Number(counts.active || 0) > 0) {
    return { label: "Evaluating", className: "status-running" };
  }
  return { label: "No usable result", className: "status-failed" };
}

function candidateResultReason(reason) {
  const messages = {
    candidate_evaluation_active: "This Candidate still has active trials.",
    not_evaluated: "No logical trials have been admitted for this candidate.",
    terminal_result_not_successful: "At least one planned trial ended without a successful result.",
    terminal_observation_missing: "A successful final attempt has no recorded result.",
    terminal_observation_not_successful: "A final result was recorded without a successful outcome.",
    primary_objective_missing_or_nonfinite: "At least one successful observation has no finite primary objective.",
    objective_aggregation_not_supported: "The configured aggregation mode cannot produce a candidate result.",
    aggregate_not_finite: "The candidate aggregate is not finite.",
    insufficient_comparators: "No second complete Candidate used the same trial settings.",
    evaluation_plan_mismatch: "Complete Candidates used different trial settings.",
  };
  return messages[String(reason || "")] || (reason ? capabilityReason(reason) : "");
}

function candidateRankingContext(page, comparison = {}) {
  const resultCapability = page && page.capabilities && page.capabilities.candidate_results || {};
  const ranking = resultCapability.ranking && typeof resultCapability.ranking === "object"
    ? resultCapability.ranking
    : {};
  const resultSummary = page && (page.result_summary || page.candidate_result_summary) || {};
  const summaryCounts = resultSummary.counts && typeof resultSummary.counts === "object"
    ? resultSummary.counts
    : {};
  const comparisonGroupCount = Number(summaryCounts.comparison_groups || 0);
  const scope = String(comparison.scope || ranking.scope || "");
  return {
    comparisonGroupCount,
    multipleComparisonGroups: comparisonGroupCount > 1,
    rankingSupported: ranking.supported === true,
    rankingEligible: ranking.supported === true && ranking.eligible === true,
    finality: comparison.finality || ranking.finality || "",
    scope,
    planScoped: ["within_evaluation_plan", "within_run_evaluation_plan"].includes(scope),
  };
}

function renderCandidateResultItem(item, page) {
  const data = item && item.data && typeof item.data === "object" ? item.data : {};
  const result = data.result && typeof data.result === "object" ? data.result : {};
  const counts = result.counts || {};
  const aggregate = result.aggregate && typeof result.aggregate === "object" ? result.aggregate : null;
  const comparison = result.comparison && typeof result.comparison === "object" ? result.comparison : {};
  const objective = result.objective || {};
  const status = candidateResultStatusPresentation(result);
  const rankingContext = candidateRankingContext(page, comparison);
  const rank = comparison.eligible && Number.isInteger(comparison.rank) ? comparison.rank : null;
  const provisional = rank != null && rankingContext.finality === "provisional_at_head";
  const notComparable = !comparison.eligible && Boolean(aggregate);
  const aggregationLabel = candidateAggregationLabel(objective.aggregation_mode) || "Aggregate";
  const aggregateLabel = `${aggregationLabel} ${objective.metric || "objective"}`;
  const logicalTrials = counts.logical_trials ?? 0;
  const usableObjectives = counts.usable_objectives ?? 0;
  const terminalFailures = Number(counts.terminal_failures || 0);
  const retries = Number(counts.retries || 0);
  const rankedCandidateCount = Number.isInteger(comparison.ranked_candidate_count) ? comparison.ranked_candidate_count : 0;
  const groupOrdinal = Number.isInteger(comparison.group_ordinal) && comparison.group_ordinal > 0 ? comparison.group_ordinal : null;
  const rankLabel = rank == null ? "Unranked" : rankingContext.multipleComparisonGroups ? "Group rank" : "Rank";
  const rankTitle = rank == null
    ? rankLabel
    : `${rankLabel} ${rank}${rankedCandidateCount > 0 ? ` of ${rankedCandidateCount}` : ""}`;
  const selection = item && item.selection || {};
  const selectionId = selection.selection_id || "";
  return `
    <button class="workbench-entity candidate-result-row candidate-result-link" data-open-candidate-route="${escapeHtml(item.id || "")}" data-workbench-selection-id="${escapeHtml(selectionId)}" type="button" aria-label="Open Candidate ${escapeHtml(item.id || "")}. ${escapeHtml(status.label)}. ${escapeHtml(aggregateLabel)} ${aggregate ? formatMetric(aggregate.value) : "not available"}. ${escapeHtml(rankTitle)}. Objective coverage ${escapeHtml(`${usableObjectives}/${logicalTrials}`)}.">
      <span class="candidate-result-summary">
        <span class="candidate-result-rank" title="${escapeHtml(rankTitle)}"><strong>${rank == null ? "—" : `#${escapeHtml(rank)}`}</strong><span>${escapeHtml(rankLabel)}</span></span>
        <span class="candidate-result-identity">
          <strong title="${escapeHtml(item.id || "")}">${escapeHtml(item.id || "-")}</strong>
          <span>${escapeHtml(data.format || "candidate")}</span>
        </span>
        <span class="candidate-result-measure">
          <span>${escapeHtml(aggregateLabel)}</span>
          <strong>${aggregate ? formatMetric(aggregate.value) : "-"}</strong>
        </span>
        <span class="candidate-result-coverage"><span>Objective coverage</span><strong>${escapeHtml(`${usableObjectives}/${logicalTrials}`)}</strong></span>
        <span class="tag-row candidate-result-tags">
          ${rankingContext.multipleComparisonGroups ? `<span class="tag candidate-result-plan">Trial group ${escapeHtml(groupOrdinal || "-")}</span>` : ""}
          <span class="status-pill ${status.className}">${escapeHtml(status.label)}</span>
          ${provisional ? `<span class="tag status-review">Provisional</span>` : ""}
          ${notComparable ? `<span class="tag status-incomplete">Not comparable</span>` : ""}
          ${terminalFailures ? `<span class="tag candidate-result-error">${escapeHtml(terminalFailures)} ${terminalFailures === 1 ? "failure" : "failures"}</span>` : ""}
          ${retries ? `<span class="tag candidate-result-warning">${escapeHtml(retries)} ${retries === 1 ? "retry" : "retries"}</span>` : ""}
        </span>
      </span>
    </button>
  `;
}

function renderIndividualObservationsPage(detail, page) {
  const items = Array.isArray(page.items) ? page.items : [];
  const paging = page.page || {};
  const emptyMessage = workbenchRunIsTerminal(detail)
    ? "No trial results were recorded; open Trials or Trial attempts to see what happened."
    : "No trial results yet; evaluations are still in progress.";
  return `
    <div class="workbench-page-heading individual-observations-heading">
      <div>
        <h3>Trial results</h3>
        <p>Each row is one recorded trial result. Candidate results combine completed trials that used the same settings.</p>
      </div>
      <span class="tag">${escapeHtml(items.length)} shown</span>
    </div>
    <div class="workbench-entity-list individual-observations-list">
      ${items.map((item) => renderObservationItem(item, page)).join("") || emptyInline(emptyMessage)}
    </div>
    ${paging.has_more ? `<button class="ghost-button run-page-more" data-run-page-more="observation" type="button" ${state.runPageLoadingKind === "observation" ? "disabled" : ""}>${state.runPageLoadingKind === "observation" ? "Loading…" : "Load more"}</button>` : ""}
  `;
}

function renderObservationItem(item, page) {
  const data = item && item.data && typeof item.data === "object" ? item.data : {};
  const selection = item && item.selection || {};
  const selectionId = selection.selection_id || "";
  const expanded = selectionId && state.expandedWorkbenchSelections.has(selectionId);
  const metric = data.objective_metric || "objective";
  const hasObjective = data.objective_value !== null && data.objective_value !== undefined && data.objective_value !== "";
  const phase = data.phase_truncated && data.phase ? `${data.phase} (truncated)` : data.phase;
  const facts = [
    ["Candidate", data.candidate_id || "-"],
    ["Logical trial", data.logical_trial_id || "-"],
    ["Attempt", data.attempt_id || "-"],
    ["Outcome", data.outcome || "-"],
    ["Phase", phase || "-"],
    ["Wall clock seconds", data.wall_clock_seconds ?? "-"],
    ["Metrics", data.metric_count ?? 0],
    ["Constraints", data.constraint_count ?? 0],
    ["Declared outputs", data.output_declaration_count ?? 0],
    ["Saved files", data.artifact_count ?? 0],
    ["Created", formatRealmTime(data.created_at) || "-"],
  ];
  return `
    <details class="workbench-entity individual-observation-row" data-workbench-selection-id="${escapeHtml(selectionId)}" ${expanded ? "open" : ""}>
      <summary class="observation-summary">
        <span class="observation-identity">
          <span class="catalog-kind-chip">trial result</span>
          <strong title="${escapeHtml(item.id || "")}">${escapeHtml(item.id || "-")}</strong>
        </span>
        <span class="observation-candidate"><span>Candidate</span><strong title="${escapeHtml(data.candidate_id || "")}">${escapeHtml(data.candidate_id || "-")}</strong></span>
        <span class="observation-measure"><span>${escapeHtml(hasObjective ? `Observed ${metric}` : "Objective")}</span><strong>${hasObjective ? formatMetric(data.objective_value) : "No objective value"}</strong></span>
        <span class="tag-row observation-tags">${statusPill(data.outcome || "unknown")}</span>
      </summary>
      ${renderSpecializedWorkbenchBody(item, page, `
        <dl class="workbench-data-grid observation-evidence">
          ${facts.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${formatCell(value)}</dd></div>`).join("")}
        </dl>
        ${renderObservationMeasurements(data)}
      `)}
    </details>
  `;
}

function renderObservationMeasurements(data) {
  const metrics = data && data.metrics && typeof data.metrics === "object" ? data.metrics : {};
  const metricRows = Array.isArray(metrics.rows) ? metrics.rows : [];
  const constraints = data && data.constraints && typeof data.constraints === "object" ? data.constraints : {};
  const constraintRows = Array.isArray(constraints.rows) ? constraints.rows : [];
  if (!metricRows.length && !constraintRows.length) return "";
  return `
    <div class="observation-measurements">
      ${metricRows.length ? `<section><h4>Recorded metrics</h4><dl>${metricRows.map((row) => `<div><dt>${escapeHtml(row.name || "metric")}${row.name_truncated ? "…" : ""}</dt><dd>${row.supported ? formatMetric(row.value) : `<span class="candidate-comparison-missing">${escapeHtml(row.reason || "Unsupported")}</span>`}</dd></div>`).join("")}</dl>${metrics.truncated ? `<small>Additional metric names are not shown.</small>` : ""}</section>` : ""}
      ${constraintRows.length ? `<section><h4>Recorded constraints</h4><dl>${constraintRows.map((row) => `<div><dt>${escapeHtml(row.name || "constraint")}${row.name_truncated ? "…" : ""}</dt><dd>${row.supported ? `<span class="tag ${row.value ? "status-ready" : "status-failed"}">${row.value ? "Satisfied" : "Violated"}</span>` : `<span class="candidate-comparison-missing">${escapeHtml(row.reason || "Unsupported")}</span>`}</dd></div>`).join("")}</dl>${constraints.truncated ? `<small>Additional constraint names are not shown.</small>` : ""}</section>` : ""}
    </div>
  `;
}

function renderSelectionTechnicalDetails(selection, item) {
  const value = selection && typeof selection === "object" ? selection : {};
  if (!Object.keys(value).length) return "";
  return `
    <details class="workbench-selection-technical">
      <summary>Technical details</summary>
      <dl class="workbench-data-grid">
        <div><dt>Selection kind</dt><dd>${escapeHtml(value.kind || item && item.kind || "entity")}</dd></div>
        <div><dt>Entity</dt><dd>${escapeHtml(value.entity_id || item && item.id || "-")}</dd></div>
        <div><dt>Run revision</dt><dd>${escapeHtml(value.revision ?? "-")}</dd></div>
        <div><dt>Sequence</dt><dd>${escapeHtml(value.sequence ?? "-")}</dd></div>
        ${value.selection_id ? `<div><dt>Selection id</dt><dd><code title="${escapeHtml(value.selection_id)}">${escapeHtml(shortDigest(value.selection_id))}</code></dd></div>` : ""}
        ${value.selection_digest ? `<div><dt>Selection digest</dt><dd><code title="${escapeHtml(value.selection_digest)}">${escapeHtml(shortDigest(value.selection_digest))}</code></dd></div>` : ""}
      </dl>
    </details>
  `;
}

function renderSpecializedWorkbenchBody(item, page, evidence) {
  const correlations = Array.isArray(item && item.correlations) ? item.correlations : [];
  const selection = item && item.selection || {};
  const selectionId = selection.selection_id || "";
  return `
    <div class="workbench-entity-body">
      ${renderSelectionActions(item, page)}
      ${selectionId ? `
        <div class="workbench-assistant-action">
          <button class="ghost-button compact-action" data-workbench-ask-assistant="${escapeHtml(selectionId)}" type="button">Ask in ${escapeHtml(assistantSessionLabel())}</button>
          <span>Send this exact Run selection to the named Assistant conversation.</span>
        </div>
      ` : ""}
      ${correlations.length ? `
        <div class="workbench-correlations">
          <strong>Correlations</strong>
          <div class="tag-row">${correlations.map(renderCorrelation).join("")}</div>
        </div>
      ` : ""}
      ${evidence}
      ${renderSelectionTechnicalDetails(selection, item)}
    </div>
  `;
}

function renderWorkbenchItem(item, page) {
  const data = item && item.data && typeof item.data === "object" ? item.data : {};
  const correlations = Array.isArray(item && item.correlations) ? item.correlations : [];
  const eligibleActions = (item && item.eligibility || page.capabilities && page.capabilities.actions || [])
    .filter((action) => action.supported && action.eligible);
  const selection = item && item.selection || {};
  const selectionId = selection.selection_id || "";
  const expanded = selectionId && state.expandedWorkbenchSelections.has(selectionId);
  return `
    <details class="workbench-entity" data-workbench-selection-id="${escapeHtml(selectionId)}" ${expanded ? "open" : ""}>
      <summary>
        <span class="workbench-entity-title">
          <span class="catalog-kind-chip">${escapeHtml(item.kind || "entity")}</span>
          <strong title="${escapeHtml(item.id || "")}">${escapeHtml(item.id || "-")}</strong>
        </span>
        <span class="tag-row">
          ${eligibleActions.map((action) => `<span class="tag">${escapeHtml(capabilityActionLabel(action.action, action.workspace_id))}</span>`).join("")}
          ${entityStateTags(data).map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join("")}
        </span>
      </summary>
      <div class="workbench-entity-body">
        ${renderSelectionActions(item, page)}
        ${selectionId ? `
          <div class="workbench-assistant-action">
            <button class="ghost-button compact-action" data-workbench-ask-assistant="${escapeHtml(selectionId)}" type="button">Ask in ${escapeHtml(assistantSessionLabel())}</button>
            <span>Send this exact Run selection to the named Assistant conversation.</span>
          </div>
        ` : ""}
        ${correlations.length ? `
          <div class="workbench-correlations">
            <strong>Correlations</strong>
            <div class="tag-row">${correlations.map(renderCorrelation).join("")}</div>
          </div>
        ` : ""}
        <dl class="workbench-data-grid">
          ${Object.entries(data).map(([key, value]) => `<div><dt>${escapeHtml(fieldLabel(key))}</dt><dd>${formatCell(value)}</dd></div>`).join("") || `<div><dt>Data</dt><dd>-</dd></div>`}
        </dl>
        ${renderSelectionTechnicalDetails(selection, item)}
      </div>
    </details>
  `;
}

function actionCapability(item, page, actionName) {
  const rowActions = Array.isArray(item && item.eligibility)
    ? item.eligibility
    : Array.isArray(item && item.actions) ? item.actions : [];
  const pageActions = Array.isArray(page && page.capabilities && page.capabilities.actions)
    ? page.capabilities.actions
    : [];
  const value = (rowActions.length
    ? rowActions.find((action) => action && action.action === actionName)
    : pageActions.find((action) => action && action.action === actionName)) || null;
  return {
    action: actionName,
    supported: Boolean(value && value.supported),
    eligible: Boolean(value && value.supported && value.eligible),
    reason: value && value.reason || `${actionName}_unavailable`,
    profiles: Array.isArray(value && value.profiles) ? value.profiles.filter((profile) => profile && profile.id) : [],
    selected_profile_id: String(value && value.selected_profile_id || ""),
    inspection_plan: value && value.inspection_plan && typeof value.inspection_plan === "object"
      ? value.inspection_plan
      : null,
    preset: value && value.preset && typeof value.preset === "object" ? value.preset : null,
    workspace_state: String(value && value.workspace_state || "not-created"),
    workspace_id: String(value && value.workspace_id || ""),
    workspace_title: String(value && value.workspace_title || ""),
  };
}

function renderSelectionActions(item, page) {
  const selection = item.selection || {};
  const selectionId = selection.selection_id || "";
  const actions = ["inspect", "open_read_only", "keep_editable", "debug_run", "environment_preview", "evaluate_child_run"]
    .map((name) => actionCapability(item, page, name))
    .filter((action) => action.supported);
  if (!actions.length) return "";
  const errors = actions
    .map((action) => state.workbenchActionErrors[workbenchActionKey(selectionId, action.action)])
    .filter(Boolean);
  const inspection = state.semanticInspections[selectionId];
  return `
    <section class="selection-actions" aria-label="Candidate actions">
      <div class="selection-actions-heading">
        <div>
          <strong>Candidate actions</strong>
          <span>View, edit, or try this Candidate without changing the Run's recorded results.</span>
        </div>
      </div>
      <div class="selection-action-list">
        ${actions.map((action) => renderSelectionActionControl(selectionId, action)).join("")}
      </div>
      ${errors.length ? `<div class="selection-action-error" role="alert">${escapeHtml(errors[0])}</div>` : ""}
      ${inspection ? renderCandidateInspection(inspection) : ""}
    </section>
  `;
}

function renderSelectionActionControl(selectionId, action) {
  const key = workbenchActionKey(selectionId, action.action);
  const pending = state.pendingWorkbenchActions.has(key)
    || (
      action.action === "open_read_only"
      && [...state.pendingWorkbenchActions].some((value) => value.endsWith(":open_read_only"))
    );
  const disabled = !selectionId || !action.eligible || pending;
  const label = capabilityActionLabel(action.action, action.workspace_id);
  const pendingLabel = action.action === "inspect"
    ? "Inspecting…"
    : action.action === "open_read_only"
    ? "Opening…"
    : action.action === "keep_editable"
    ? action.workspace_id ? "Opening…" : "Creating…"
    : action.action === "debug_run"
    ? "Starting…"
    : action.action === "evaluate_child_run"
    ? "Creating…"
    : "Opening…";
  const buttonClass = ["debug_run", "evaluate_child_run"].includes(action.action) ? "primary-button" : "ghost-button";
  const previewProfiles = action.action === "environment_preview" ? action.profiles : [];
  const selectedPreviewProfile = selectedPreviewProfileId(action, selectionId);
  return `
    <div class="selection-action-control ${action.eligible ? "eligible" : "unavailable"}">
      ${previewProfiles.length ? `
        <label class="candidate-preview-profile">
          <span>Interface profile</span>
          <select data-preview-profile-selection="${escapeHtml(selectionId)}" ${pending ? "disabled" : ""}>
            ${previewProfiles.map((profile) => `<option value="${escapeHtml(profile.id)}" ${profile.id === selectedPreviewProfile ? "selected" : ""}>${escapeHtml(profile.label || profile.id)}</option>`).join("")}
          </select>
        </label>
      ` : ""}
      <button class="${buttonClass} compact-action" data-workbench-action="${escapeHtml(action.action)}" data-workbench-selection="${escapeHtml(selectionId)}" type="button" ${disabled ? "disabled" : ""} title="${escapeHtml(action.eligible ? label : capabilityReason(action.reason))}">${escapeHtml(pending ? pendingLabel : label)}</button>
      ${!action.eligible ? `<span>Unavailable: ${escapeHtml(capabilityReason(action.reason))}</span>` : `<span>${escapeHtml(selectionActionDescription(action.action, action.workspace_id))}</span>`}
    </div>
  `;
}

function selectionActionDescription(actionName, workspaceId = "") {
  if (actionName === "inspect") return "View saved Candidate values and Environment details.";
  if (actionName === "open_read_only") return "Browse saved files read-only without creating a Workspace.";
  if (actionName === "keep_editable") return workspaceId
    ? "Open the editable Workspace already linked to this Candidate."
    : "Create an editable Workspace for this Candidate.";
  if (actionName === "debug_run") return "Evaluate this Candidate once without opening an interface or changing the source Run.";
  if (actionName === "environment_preview") return "Open the Environment's interactive interface with this exact saved Candidate loaded.";
  if (actionName === "evaluate_child_run") return "Evaluate this exact Candidate again with the same trial settings in a separate recorded Run.";
  return "Use this exact saved item.";
}

function selectedPreviewProfileId(action, selectionId) {
  if (!action || action.action !== "environment_preview") return "";
  const profiles = Array.isArray(action.profiles) ? action.profiles : [];
  const eligibleIds = new Set(
    profiles.map((profile) => String(profile && profile.id || "")).filter(Boolean),
  );
  const choices = [
    state.environmentPreviewProfileSelections[selectionId],
    action.selected_profile_id,
    profiles[0] && profiles[0].id,
  ];
  return String(choices.find((value) => value && eligibleIds.has(String(value))) || "");
}

function workbenchActionKey(selectionId, action) {
  return `${selectionId || "missing"}:${action}`;
}

function durableWorkbenchIntentKey(runId, actionName, item, confirmation = null) {
  if (actionName !== "evaluate_child_run") return "";
  const selection = item && item.selection || {};
  const planDigest = confirmation && confirmation.preset && confirmation.preset.plan_digest || "";
  return [runId, actionName, item && item.id || "", selection.selection_digest || "", planDigest].join(":");
}

function durableWorkbenchRequestId(intentKey) {
  if (!intentKey) return newRequestId();
  if (!state.workbenchActionRequestIds[intentKey]) {
    state.workbenchActionRequestIds[intentKey] = newRequestId();
    storeValue(STORAGE_KEYS.durableActionIntents, JSON.stringify(state.workbenchActionRequestIds));
  }
  return state.workbenchActionRequestIds[intentKey];
}

function completeDurableWorkbenchIntent(intentKey) {
  if (!intentKey || !state.workbenchActionRequestIds[intentKey]) return;
  delete state.workbenchActionRequestIds[intentKey];
  storeValue(STORAGE_KEYS.durableActionIntents, JSON.stringify(state.workbenchActionRequestIds));
}

function currentWorkbenchItem(selectionId) {
  const pages = state.selectedRun && state.selectedRun.pages || {};
  for (const kind of ["candidate", "logical_trial", "attempt", "observation", "artifact"]) {
    const item = (pages[kind] && pages[kind].items || []).find(
      (candidate) => candidate && candidate.selection && candidate.selection.selection_id === selectionId,
    );
    if (item) return item;
  }
  const focused = routedCandidateResolution();
  if (
    focused
    && focused.candidate
    && focused.candidate.selection
    && focused.candidate.selection.selection_id === selectionId
  ) return focused.candidate;
  return null;
}

function startCandidateTry(selectionId, trigger = null) {
  const item = currentWorkbenchItem(selectionId);
  const page = item ? workbenchPage(state.selectedRun, item.kind) : null;
  if (!item || !page) return;
  const modes = ["debug_run", "environment_preview"]
    .map((actionName) => actionCapability(item, page, actionName))
    .filter((capability) => capability.supported);
  if (!modes.length) return;
  state.candidateTryNotice = "";
  const staleNotice = els.runDetail && els.runDetail.querySelector("[data-candidate-try-notice]");
  if (staleNotice) staleNotice.remove();
  const directMode = directCandidateTryMode(modes);
  if (directMode) {
    performWorkbenchAction(directMode.action, selectionId, {
      restoreCandidateTryFocus: true,
    });
    return;
  }
  const eligibleModes = modes.filter((mode) => mode.eligible);
  const selectedAction = eligibleModes.some((mode) => mode.action === "debug_run")
    ? "debug_run"
    : eligibleModes[0] && eligibleModes[0].action || modes[0].action;
  state.pendingCandidateTry = {
    run_id: selectedCanonicalRunId(),
    run_head: { ...(state.selectedRun && state.selectedRun.workbench && state.selectedRun.workbench.head || {}) },
    selection_id: selectionId,
    candidate_id: String(item.id || item.selection && item.selection.entity_id || "Candidate"),
    modes,
    selected_action: selectedAction,
  };
  state.candidateTryReturnFocus = trigger && typeof trigger.focus === "function"
    ? trigger
    : document.activeElement;
  renderCandidateTrySheet();
}

function renderCandidateTrySheet() {
  const pending = state.pendingCandidateTry;
  if (!els.candidateTryModal || !els.candidateTryBody) return;
  if (!pending) {
    els.candidateTryModal.hidden = true;
    return;
  }
  const selectedMode = pending.modes.find((mode) => mode.action === pending.selected_action)
    || pending.modes[0];
  const eligibleModes = pending.modes.filter((mode) => mode.eligible);
  const hasEligibleMode = eligibleModes.length > 0;
  if (els.candidateTryTitle) {
    els.candidateTryTitle.textContent = hasEligibleMode ? "Try Candidate" : "Candidate cannot be tried";
  }
  if (els.candidateTryIntro) {
    els.candidateTryIntro.textContent = hasEligibleMode
      ? "Choose how to try this Candidate. The source Run and its recorded results will not change."
      : "This Candidate cannot be tried with the current setup. The reasons are shown below.";
  }
  if (els.candidateTryCloseButton) {
    els.candidateTryCloseButton.setAttribute(
      "aria-label",
      hasEligibleMode ? "Close Candidate try options" : "Close unavailable Candidate explanation",
    );
  }
  pending.selected_action = selectedMode.action;
  const selectedProfile = selectedPreviewProfileId(
    selectedMode,
    pending.selection_id,
  );
  if (selectedProfile) {
    state.environmentPreviewProfileSelections[pending.selection_id] = selectedProfile;
  }
  els.candidateTryBody.innerHTML = `
    <p>${hasEligibleMode
      ? `Select how to try <strong>${escapeHtml(pending.candidate_id)}</strong>.`
      : `<strong>${escapeHtml(pending.candidate_id)}</strong> cannot currently be tried. The reasons are shown below.`}</p>
    <fieldset class="candidate-try-mode-list">
      <legend>${hasEligibleMode ? "Ways to try it" : "Why it is unavailable"}</legend>
      ${pending.modes.map((mode) => mode.eligible ? `
          <label class="candidate-try-mode ${mode.action === selectedMode.action ? "selected" : ""}">
            <input type="radio" name="candidateTryMode" value="${escapeHtml(mode.action)}" ${mode.action === selectedMode.action ? "checked" : ""}>
            <span>
              <strong>${escapeHtml(capabilityActionLabel(mode.action))}</strong>
              <small>${escapeHtml(selectionActionDescription(mode.action))}</small>
            </span>
          </label>
        ` : `
          <div class="candidate-try-mode unavailable" aria-disabled="true">
            <span>
              <span class="candidate-try-mode-heading"><strong>${escapeHtml(capabilityActionLabel(mode.action))}</strong><span class="tag status-unavailable">Unavailable</span></span>
              <small>${escapeHtml(capabilityReason(mode.reason))}</small>
            </span>
          </div>
        `).join("")}
    </fieldset>
    ${selectedMode.eligible && selectedMode.action === "environment_preview" && selectedMode.profiles.length ? `
      <label class="candidate-try-profile">
        <span>Interface</span>
        <select data-candidate-try-profile>
          ${selectedMode.profiles.map((profile) => `<option value="${escapeHtml(profile.id)}" ${profile.id === selectedProfile ? "selected" : ""}>${escapeHtml(profile.label || profile.id)}</option>`).join("")}
        </select>
      </label>
    ` : ""}
    ${selectedMode.eligible ? renderCandidateInspectionPlan(selectedMode.inspection_plan, {
      selectedProfile,
      result: false,
    }) : ""}
    ${hasEligibleMode ? `<div class="operator-job-notice">If the try produces useful results, you can save them to the Shortlist or save an output folder as a Workspace.</div>` : ""}
  `;
  if (els.candidateTrySubmitButton) {
    els.candidateTrySubmitButton.hidden = !hasEligibleMode;
    els.candidateTrySubmitButton.disabled = !selectedMode.eligible;
    els.candidateTrySubmitButton.textContent = selectedMode.eligible
      ? candidateTrySubmitLabel(selectedMode.action)
      : "Unavailable";
  }
  if (els.candidateTryCancelButton) {
    els.candidateTryCancelButton.hidden = !hasEligibleMode;
    els.candidateTryCancelButton.textContent = "Cancel";
  }
  if (els.candidateTryActions) els.candidateTryActions.hidden = !hasEligibleMode;
  els.candidateTryModal.hidden = false;
  window.requestAnimationFrame(() => {
    const selected = els.candidateTryBody.querySelector("input[name='candidateTryMode']:checked");
    if (selected) selected.focus();
    else if (els.candidateTryCloseButton) els.candidateTryCloseButton.focus();
  });
}

function updateCandidateTrySheet(event) {
  const pending = state.pendingCandidateTry;
  if (!pending) return;
  const mode = event.target && event.target.closest && event.target.closest("input[name='candidateTryMode']");
  if (mode && pending.modes.some((item) => item.action === mode.value)) {
    pending.selected_action = mode.value;
    renderCandidateTrySheet();
    return;
  }
  const profile = event.target && event.target.closest && event.target.closest("[data-candidate-try-profile]");
  if (profile) {
    state.environmentPreviewProfileSelections[pending.selection_id] = profile.value;
    const planProfile = els.candidateTryBody.querySelector("[data-candidate-try-plan-profile]");
    if (planProfile) planProfile.textContent = profile.value;
  }
}

function renderCandidateInspectionPlan(rawPlan, options = {}) {
  const plan = rawPlan && typeof rawPlan === "object" ? rawPlan : null;
  if (!plan || plan.schema !== "optpilot.candidate-try-plan.v1") return "";
  const environment = plan.environment && typeof plan.environment === "object"
    ? plan.environment
    : {};
  const settings = plan.settings && typeof plan.settings === "object"
    ? plan.settings
    : {};
  const mode = String(plan.mode || "");
  const selectedProfile = String(options.selectedProfile || settings.interface_profile_id || "");
  const rows = [];
  if (environment.id) {
    rows.push(`<div><dt>Environment</dt><dd>${escapeHtml(environment.id)}</dd></div>`);
  }
  if (environment.revision) {
    rows.push(`<div><dt>Environment version</dt><dd title="${escapeHtml(environment.revision)}">${escapeHtml(shortDigest(environment.revision))}</dd></div>`);
  }
  if (mode === "try_once") {
    if (Object.prototype.hasOwnProperty.call(settings, "seed")) {
      const seedLabel = settings.seed === null
        ? "Not set"
        : childRunSeedLabel(settings.seed);
      rows.push(`<div><dt>Seed</dt><dd>${escapeHtml(seedLabel)}</dd></div>`);
    }
    if (Number.isInteger(settings.repetition_index)) {
      rows.push(`<div><dt>Repetition</dt><dd>${escapeHtml(settings.repetition_index)}</dd></div>`);
    }
  }
  if (mode === "try_interactively" && selectedProfile) {
    rows.push(`<div><dt>Interface profile</dt><dd data-candidate-try-plan-profile>${escapeHtml(selectedProfile)}</dd></div>`);
  }
  if (!rows.length) return "";
  return `
    <section class="candidate-try-plan" aria-label="Settings for this try">
      <div>
        <strong>Settings for this try</strong>
        <span>${options.result ? "Settings used for this try." : "Fixed for this try."}</span>
      </div>
      <dl>${rows.join("")}</dl>
    </section>
  `;
}

function closeCandidateTrySheet({ restoreFocus = true } = {}) {
  state.pendingCandidateTry = null;
  if (els.candidateTryModal) els.candidateTryModal.hidden = true;
  if (els.candidateTrySubmitButton) {
    els.candidateTrySubmitButton.hidden = false;
    els.candidateTrySubmitButton.disabled = false;
    els.candidateTrySubmitButton.textContent = "Run headless";
  }
  if (els.candidateTryCancelButton) {
    els.candidateTryCancelButton.hidden = false;
    els.candidateTryCancelButton.textContent = "Cancel";
  }
  if (els.candidateTryActions) els.candidateTryActions.hidden = false;
  const target = state.candidateTryReturnFocus;
  state.candidateTryReturnFocus = null;
  if (restoreFocus && target && typeof target.focus === "function" && document.contains(target)) {
    window.requestAnimationFrame(() => target.focus());
  }
}

function confirmCandidateTry() {
  const pending = state.pendingCandidateTry;
  if (!pending) return;
  const actionName = pending.selected_action;
  const selectedMode = pending.modes.find((mode) => mode.action === actionName);
  if (!selectedMode || !selectedMode.eligible) return;
  const currentHead = state.selectedRun && state.selectedRun.workbench && state.selectedRun.workbench.head || {};
  const contextMatches = pending.run_id === selectedCanonicalRunId()
    && pending.candidate_id === String(state.routedCandidateId || "")
    && Number(pending.run_head && pending.run_head.revision) === Number(currentHead.revision)
    && Number(pending.run_head && pending.run_head.sequence) === Number(currentHead.sequence);
  if (!contextMatches) {
    closeCandidateTrySheet({ restoreFocus: false });
    return;
  }
  const selectionId = pending.selection_id;
  closeCandidateTrySheet({ restoreFocus: false });
  performWorkbenchAction(actionName, selectionId, {
    restoreCandidateTryFocus: true,
  });
}

function restoreFocusedCandidateTryFocus(selectionId, destination, jobId = "") {
  window.requestAnimationFrame(() => {
    if (!state.routedCandidateId || state.activeRunTab !== "candidate" || !els.runDetail) return;
    const item = currentWorkbenchItem(selectionId);
    if (!item || String(item.id || "") !== String(state.routedCandidateId)) return;
    let target = null;
    if (destination === "job" && jobId) {
      target = [...els.runDetail.querySelectorAll("[data-operator-job-id]")]
        .find((button) => button.dataset.operatorJobId === jobId);
    } else if (destination === "retry") {
      target = [...els.runDetail.querySelectorAll("[data-try-candidate]")]
        .find((button) => button.dataset.tryCandidate === selectionId);
    } else if (destination === "notice") {
      target = els.runDetail.querySelector("[data-candidate-try-notice]");
    } else {
      target = els.runDetail.querySelector("[data-candidate-try-status]");
    }
    if (target && typeof target.focus === "function") target.focus();
  });
}

function handleCandidateTrySheetKeydown(event) {
  if (!state.pendingCandidateTry || !els.candidateTryModal) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeCandidateTrySheet();
    return;
  }
  trapModalFocus(event, els.candidateTryModal, els.candidateTryDialog);
}

function trapModalFocus(event, modal, fallback) {
  if (event.key !== "Tab" || !modal) return;
  const focusable = [...modal.querySelectorAll(
    "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])",
  )].filter((element) => !element.hidden && !element.closest("[hidden]"));
  if (!focusable.length) {
    event.preventDefault();
    if (fallback && typeof fallback.focus === "function") fallback.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const activeIsFocusable = focusable.includes(document.activeElement);
  if (event.shiftKey && (document.activeElement === first || !activeIsFocusable)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !activeIsFocusable)) {
    event.preventDefault();
    first.focus();
  }
}

function openChildRunConfirmation(selectionId, capability) {
  const runId = selectedCanonicalRunId();
  const item = currentWorkbenchItem(selectionId);
  const preset = capability && capability.preset;
  const coordinates = preset && Array.isArray(preset.coordinates) ? preset.coordinates : [];
  const logicalTrials = Number(preset && preset.logical_trials);
  const maxTrials = Number(preset && preset.max_trials);
  if (
    !runId || !item || !item.selection || !preset
    || preset.schema !== "optpilot.re-evaluate-exact-plan-preset.v1"
    || preset.id !== "re_evaluate_exact_plan"
    || preset.parent_run_id !== runId
    || !preset.parent_seal_digest || !preset.plan_digest
    || !preset.candidate || !preset.candidate.id || !preset.candidate.format
    || !preset.environment || !preset.environment.id || !preset.environment.revision
    || !Number.isInteger(logicalTrials) || logicalTrials <= 0 || logicalTrials > 100
    || maxTrials !== logicalTrials || coordinates.length !== logicalTrials
    || preset.method_proposals !== false
  ) {
    const key = workbenchActionKey(selectionId, "evaluate_child_run");
    state.workbenchActionErrors[key] = "The re-evaluation plan is incomplete. Refresh the Run and try again.";
    renderRunDetail();
    return;
  }
  const activeElement = document.activeElement;
  state.childRunReturnFocus = activeElement && activeElement !== document.body
    ? activeElement
    : null;
  state.pendingChildRunConfirmation = {
    run_id: runId,
    selection_id: selectionId,
    selection: item.selection,
    candidate_id: preset.candidate.id,
    candidate_format: preset.candidate.format,
    preset,
  };
  renderChildRunConfirmation();
  window.requestAnimationFrame(() => {
    if (els.childRunConfirmationSubmitButton) els.childRunConfirmationSubmitButton.focus();
  });
}

function closeChildRunConfirmation(options = {}) {
  const returnFocus = state.childRunReturnFocus;
  const selectionId = state.pendingChildRunConfirmation
    && state.pendingChildRunConfirmation.selection_id;
  state.pendingChildRunConfirmation = null;
  state.childRunReturnFocus = null;
  if (els.childRunConfirmationModal) els.childRunConfirmationModal.hidden = true;
  if (options.restoreFocus !== false) {
    window.requestAnimationFrame(() => {
      const fallback = [...document.querySelectorAll("[data-workbench-action='evaluate_child_run']")]
        .find((button) => button.dataset.workbenchSelection === selectionId);
      const target = returnFocus && returnFocus.isConnected ? returnFocus : fallback;
      if (target && typeof target.focus === "function") target.focus();
    });
  }
}

function handleChildRunConfirmationKeydown(event) {
  const pending = state.pendingChildRunConfirmation;
  if (!pending) return;
  const key = workbenchActionKey(pending.selection_id, "evaluate_child_run");
  const submitting = state.pendingWorkbenchActions.has(key);
  if (event.key === "Escape" && !submitting) {
    event.preventDefault();
    closeChildRunConfirmation();
    return;
  }
  trapModalFocus(event, els.childRunConfirmationModal, els.childRunConfirmationDialog);
}

function renderChildRunConfirmation() {
  const pending = state.pendingChildRunConfirmation;
  if (!els.childRunConfirmationModal || !els.childRunConfirmationBody) return;
  if (!pending) {
    els.childRunConfirmationModal.hidden = true;
    return;
  }
  const preset = pending.preset;
  const objective = preset.objective && typeof preset.objective === "object" ? preset.objective : {};
  const environment = preset.environment && typeof preset.environment === "object" ? preset.environment : {};
  const coordinates = Array.isArray(preset.coordinates) ? preset.coordinates : [];
  if (els.childRunConfirmationTitle) {
    els.childRunConfirmationTitle.textContent = `Evaluate Candidate ${pending.candidate_id} again?`;
  }
  els.childRunConfirmationBody.innerHTML = `
    <section class="child-run-confirmation-summary" aria-label="Re-evaluation plan summary">
      <div><span>Source Run</span><strong title="${escapeHtml(pending.run_id)}">${escapeHtml(pending.run_id)}</strong></div>
      <div><span>Candidate</span><strong>${escapeHtml(pending.candidate_id)}</strong></div>
      <div><span>Candidate format</span><strong>${escapeHtml(pending.candidate_format)}</strong></div>
      <div><span>Environment</span><strong>${escapeHtml(environment.id)}</strong></div>
      <div><span>Environment version</span><strong title="${escapeHtml(environment.revision)}">${escapeHtml(shortDigest(environment.revision))}</strong></div>
      <div><span>Objective</span><strong>${escapeHtml([objective.metric, objective.direction].filter(Boolean).join(" · ") || "Source Run objective")}</strong></div>
      <div><span>Trial settings</span><strong>${escapeHtml(preset.logical_trials)} fixed seed/repetition ${preset.logical_trials === 1 ? "pair" : "pairs"}</strong></div>
      <div><span>Method</span><strong>Not run — the Candidate is unchanged</strong></div>
    </section>
    <section class="child-run-confirmation-policy">
      <h3>What this creates</h3>
      <ul>
        <li>A new Run with its own trial results and budget.</li>
        <li>The exact Candidate, Environment version, seeds, repetitions, and trial settings are reused.</li>
        <li>The source Run is unchanged. The new Run records its own results.</li>
      </ul>
    </section>
    <details class="child-run-coordinate-list">
      <summary>Technical details</summary>
      <dl>
        <div><dt>Source Run seal</dt><dd><code title="${escapeHtml(preset.parent_seal_digest)}">${escapeHtml(shortDigest(preset.parent_seal_digest))}</code></dd></div>
        <div><dt>Evaluation plan</dt><dd><code title="${escapeHtml(preset.plan_digest)}">${escapeHtml(shortDigest(preset.plan_digest))}</code></dd></div>
      </dl>
      <h3>Seeds and repetitions</h3>
      <ol>${coordinates.map((coordinate) => `<li>seed ${escapeHtml(childRunSeedLabel(coordinate.seed))} · repetition ${escapeHtml(coordinate.repetition_index)}</li>`).join("")}</ol>
    </details>
  `;
  const key = workbenchActionKey(pending.selection_id, "evaluate_child_run");
  if (els.childRunConfirmationSubmitButton) {
    els.childRunConfirmationSubmitButton.disabled = state.pendingWorkbenchActions.has(key);
    els.childRunConfirmationSubmitButton.textContent = state.pendingWorkbenchActions.has(key) ? "Starting…" : "Start re-evaluation";
  }
  els.childRunConfirmationModal.hidden = false;
}

function shortDigest(value) {
  const text = String(value || "");
  const digest = text.startsWith("sha256:") ? text.slice(7) : text;
  return digest.length > 20 ? `${digest.slice(0, 16)}…` : digest || "-";
}

function childRunSeedLabel(value) {
  try {
    const encoded = JSON.stringify(value);
    return encoded && encoded.length <= 120 ? encoded : "(structured seed too long to show)";
  } catch (error) {
    return "(unavailable)";
  }
}

async function confirmChildRunCreation() {
  const pending = state.pendingChildRunConfirmation;
  if (!pending) return;
  await performWorkbenchAction("evaluate_child_run", pending.selection_id, { confirmed: true });
}

async function askAssistantAboutWorkbenchSelection(selectionId) {
  const runId = selectedCanonicalRunId();
  const item = currentWorkbenchItem(selectionId);
  if (!runId || !item || !item.selection) return;
  state.assistantMode = "chat";
  setAssistantOpen(true);
  const session = currentAgentSession();
  if (!session || !session.id || session.id.startsWith("agent-session-")) {
    pushAssistantMessage([
      "tool",
      "Selection unavailable",
      "Start an Assistant conversation, then choose the Assistant action again.",
    ], { persist: false });
    renderAssistant();
    return;
  }
  try {
    const payload = await postJson(
      `/api/agent-sessions/${encodeURIComponent(session.id)}/run-selection`,
      {
        schema: "optpilot.assistant-run-selection-request.v1",
        presentation_selection: item.selection,
      },
    );
    if (
      currentAgentSession() && currentAgentSession().id === session.id
      && selectedCanonicalRunId() === runId
    ) {
      state.assistantRunSelection = {
        session_id: session.id,
        run_id: payload.run_id,
        handle: payload.handle,
        kind: payload.selection && payload.selection.kind || "entity",
        id: payload.selection && payload.selection.entity_id || "",
        head: payload.head || null,
      };
      if (els.agentInput) {
        els.agentInput.value = `Help me inspect this ${state.assistantRunSelection.kind} from run ${runId}. Explain what it shows, relate it to the objective and its correlated records, and call out any evidence or failures I should inspect next.`;
        els.agentInput.dataset.touched = "true";
        els.agentInput.focus();
        els.agentInput.select();
      }
    }
  } catch (error) {
    state.assistantRunSelection = null;
    pushAssistantMessage([
      "tool",
      "Selection unavailable",
      boundedPublicActionError(
        error,
        "The run item changed or is no longer available. Choose the Assistant action again.",
      ),
    ], { persist: false });
    renderAssistant();
  }
}

async function performWorkbenchAction(actionName, selectionId, options = {}) {
  const runId = selectedCanonicalRunId();
  const item = currentWorkbenchItem(selectionId);
  const page = item ? workbenchPage(state.selectedRun, item.kind) : null;
  if (!runId || !item) return;
  const capability = actionCapability(item, page, actionName);
  if (actionName === "evaluate_child_run" && !options.confirmed) {
    if (capability.eligible) openChildRunConfirmation(selectionId, capability);
    return;
  }
  const confirmedChildPlan = actionName === "evaluate_child_run"
    ? state.pendingChildRunConfirmation
    : null;
  if (
    actionName === "evaluate_child_run"
    && (
      !confirmedChildPlan
      || confirmedChildPlan.run_id !== runId
      || confirmedChildPlan.selection_id !== selectionId
      || !capability.preset
      || capability.preset.parent_seal_digest !== confirmedChildPlan.preset.parent_seal_digest
      || capability.preset.plan_digest !== confirmedChildPlan.preset.plan_digest
    )
  ) {
    closeChildRunConfirmation();
    state.workbenchActionErrors[workbenchActionKey(selectionId, actionName)] = "The Run or evaluation plan changed. Review the refreshed plan before re-evaluating.";
    renderRunDetail();
    return;
  }
  const key = workbenchActionKey(selectionId, actionName);
  const durableIntentKey = durableWorkbenchIntentKey(
    runId, actionName, item, confirmedChildPlan,
  );
  if (
    actionName === "open_read_only"
    && [...state.pendingWorkbenchActions].some((value) => value.endsWith(":open_read_only"))
  ) return;
  if (!capability.eligible || state.pendingWorkbenchActions.has(key)) return;
  const restoreCandidateTryFocus = Boolean(
    options.restoreCandidateTryFocus
    && ["debug_run", "environment_preview"].includes(actionName),
  );
  let createdOperatorJobId = "";
  state.pendingWorkbenchActions.add(key);
  state.expandedWorkbenchSelections.add(selectionId);
  delete state.workbenchActionErrors[key];
  renderRunDetail();
  if (restoreCandidateTryFocus) {
    restoreFocusedCandidateTryFocus(selectionId, "status");
  }
  try {
    const requestId = durableWorkbenchRequestId(durableIntentKey);
    const previewProfileId = selectedPreviewProfileId(capability, selectionId);
    if (actionName === "open_read_only" && state.selectionContentView) {
      await closeSelectionContentView({ silent: true });
    }
    const parameters = actionName === "environment_preview"
      ? { profile_id: previewProfileId }
      : actionName === "open_read_only"
      ? { content_session_id: state.selectionContentSessionId || null }
      : actionName === "evaluate_child_run"
      ? {
          schema: "optpilot.re-evaluate-exact-plan-confirmation.v1",
          preset: "re_evaluate_exact_plan",
          expected_parent_seal_digest: confirmedChildPlan.preset.parent_seal_digest,
          expected_plan_digest: confirmedChildPlan.preset.plan_digest,
        }
      : {};
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/actions`, {
      schema: "optpilot.run-workbench-action-request.v1",
      request_id: requestId,
      action: actionName,
      presentation_selection: item.selection,
      parameters,
    });
    if (!payload || payload.run_id !== runId || payload.action !== actionName) {
      throw new Error("Candidate action response does not match this Candidate.");
    }
    if (actionName === "inspect") {
      if (!payload.inspection || typeof payload.inspection !== "object") throw new Error("Inspection response is incomplete.");
      state.semanticInspections[selectionId] = payload.inspection;
    } else if (actionName === "open_read_only") {
      await openSelectionContentView(payload, item, runId);
    } else if (actionName === "keep_editable") {
      if (!payload.workspace || typeof payload.workspace !== "object") throw new Error("Kept workspace response is incomplete.");
      const workspace = mergeUiWorkspace(payload.workspace);
      if (!workspace) throw new Error("Kept workspace response is incomplete.");
      keepWorkspaceSelected(workspace.id);
      setView("workspace");
    } else if (actionName === "evaluate_child_run") {
      const childRunId = payload.child_run && String(payload.child_run.run_id || "");
      if (!childRunId || childRunId === runId) throw new Error("Child run response is incomplete.");
      closeChildRunConfirmation({ restoreFocus: false });
      state.activeRunTab = "overview";
      await loadRunDetail(childRunId);
    } else {
      const job = operatorJobFromPayload(payload);
      if (!job) throw new Error(`${capabilityActionLabel(actionName)} did not return a Candidate try.`);
      if (operatorJobRunId(job) && operatorJobRunId(job) !== runId) throw new Error("Candidate try belongs to another Run.");
      selectOperatorJobsRun(runId);
      upsertOperatorJob(job);
      state.selectedOperatorJobId = job.job_id;
      state.selectedOperatorJob = job;
      createdOperatorJobId = job.job_id;
    }
    completeDurableWorkbenchIntent(durableIntentKey);
  } catch (error) {
    state.workbenchActionErrors[key] = boundedPublicActionError(error, `${capabilityActionLabel(actionName)} could not be completed.`);
  } finally {
    state.pendingWorkbenchActions.delete(key);
    if (actionName === "evaluate_child_run") renderChildRunConfirmation();
    if (state.selectedRunId === runId) renderRunDetail();
    if (restoreCandidateTryFocus) {
      restoreFocusedCandidateTryFocus(
        selectionId,
        createdOperatorJobId ? "job" : "retry",
        createdOperatorJobId,
      );
    }
    if (!["inspect", "open_read_only", "keep_editable", "evaluate_child_run"].includes(actionName)) loadSelectedRunOperatorJobs({ silent: true });
  }
}

function renderCandidateInspection(inspection) {
  const candidate = inspection.candidate || {};
  const environment = inspection.environment || {};
  const evaluation = inspection.evaluation || {};
  const realization = inspection.realization || {};
  const specRows = candidate.spec_included ? safeCandidateSpecRows(candidate.spec) : [];
  return `
    <div class="candidate-inspection" role="region" aria-label="Candidate inspection">
      <div class="candidate-inspection-heading">
        <div>
          <span class="eyebrow">Saved Candidate details</span>
          <strong>${escapeHtml(candidate.candidate_id || "Candidate")}</strong>
        </div>
        <span class="tag">Read-only</span>
      </div>
      <div class="candidate-inspection-grid">
        ${semanticPanel("Candidate", [
          ["Format", candidate.format],
          ["Content items", candidate.content_count],
          ["Specification", candidate.spec_included ? "available values included" : "not available"],
        ])}
        ${semanticPanel("Environment", [
          ["Environment", environment.environment_id],
          ["Availability", environment.availability],
        ])}
        ${semanticPanel("Evaluation", [
          ["Environment evaluator", evaluation.runnable == null ? null : evaluation.runnable ? "Configured" : "Not configured"],
          ["Objective", evaluation.objective_metric],
          ["Direction", evaluation.objective_direction],
          ["Default seed", evaluation.default_seed],
        ])}
      </div>
      ${specRows.length ? `<section class="candidate-inspection-spec"><h5>Candidate values</h5><dl>${specRows.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl></section>` : ""}
      <details class="candidate-inspection-realization">
        <summary>Technical details</summary>
        <dl class="workbench-data-grid">
          <div><dt>Runtime kind</dt><dd>${escapeHtml(environment.runtime_kind || "Not reported")}</dd></div>
          <div><dt>Portability</dt><dd>${escapeHtml(environment.portability || "Not reported")}</dd></div>
          <div><dt>Temporary files</dt><dd>${realization.workspace_created ? "Prepared" : "Not prepared"}</dd></div>
          <div><dt>Content</dt><dd>${realization.content_copied ? "Copied" : "Not copied"}</dd></div>
          <div><dt>Process</dt><dd>${realization.process_started ? "Started" : "Not started"}</dd></div>
        </dl>
      </details>
    </div>
  `;
}

function semanticPanel(title, rows) {
  const visible = rows.filter(([, value]) => value !== null && value !== undefined && value !== "");
  return `
    <section class="candidate-semantic-panel">
      <h5>${escapeHtml(title)}</h5>
      <dl>${visible.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("") || `<div><dt>Details</dt><dd>Not projected</dd></div>`}</dl>
    </section>
  `;
}

function safeCandidateSpecRows(value, prefix = "", rows = [], depth = 0) {
  if (rows.length >= 24 || depth > 3 || value == null) return rows;
  if (Array.isArray(value)) {
    value.slice(0, 12).forEach((item, index) => safeCandidateSpecRows(item, `${prefix}[${index}]`, rows, depth + 1));
    return rows;
  }
  if (typeof value === "object") {
    Object.entries(value).slice(0, 32).forEach(([key, item]) => {
      if (candidateSpecKeyIsSensitive(key) || rows.length >= 24) return;
      safeCandidateSpecRows(item, prefix ? `${prefix}.${key}` : key, rows, depth + 1);
    });
    return rows;
  }
  const text = String(value);
  if (!prefix || candidateSpecValueIsPath(text)) return rows;
  rows.push([prefix, text.length > 160 ? `${text.slice(0, 157)}…` : text]);
  return rows;
}

function candidateSpecKeyIsSensitive(key) {
  return /(^|[_\s-])(command|cmd|entrypoint|executable|shell|script|args|path|cwd|directory|provider|backend|secret|token|credential|password|passphrase|api[_-]?key|access[_-]?key|private[_-]?key|cookie|environment[_-]?variable|owner|principal|lease|binding|launch|port)([_\s-]|$)/i.test(String(key || ""));
}

function candidateSpecValueIsPath(value) {
  const text = String(value || "").trim();
  return /^(?:file:|[~/]|[a-zA-Z]:[\\/])/.test(text);
}

function boundedPublicActionError(error, fallback) {
  const message = String(error && error.message || error || "").trim();
  if (!message) return fallback;
  const unsafe = /(?:file:\/\/|\/(?:Users|home|private|tmp|var|etc)\/|[a-zA-Z]:[\\/]|(?:secret|token|password|credential|command|cwd|host_path|workspace_path)\s*[:=])/i;
  if (unsafe.test(message)) return fallback;
  return message.length > 320 ? `${message.slice(0, 317)}…` : message;
}

async function openSelectionContentView(payload, item, runId, options = {}) {
  const raw = payload && payload.content_view;
  const selection = item && item.selection || {};
  if (!raw || raw.schema !== "optpilot.selection-content-view.v1") {
    throw new Error("Read-only content response is incomplete.");
  }
  const handle = String(raw.handle || "");
  const contentSessionId = String(raw.content_session_id || "");
  const contentKind = String(raw.content_kind || "");
  const selected = raw.selection && typeof raw.selection === "object" ? raw.selection : {};
  const head = raw.head && typeof raw.head === "object" ? raw.head : {};
  if (!/^scv_[a-f0-9]{32}$/.test(handle) || !/^scs_[a-f0-9]{32}$/.test(contentSessionId)) {
    throw new Error("Read-only content authority is invalid.");
  }
  if (!["tree", "blob"].includes(contentKind)) {
    throw new Error("Read-only content kind is unsupported.");
  }
  const revision = Number(head.revision);
  const sequence = head.sequence === null ? null : Number(head.sequence);
  if (
    !Number.isSafeInteger(revision) || revision < 0
    || (sequence !== null && (!Number.isSafeInteger(sequence) || sequence < 0))
  ) {
    throw new Error("Read-only content head is invalid.");
  }
  const requireExactHead = options.requireExactHead !== false;
  if (
    String(selected.kind || "") !== String(selection.kind || item.kind || "")
    || String(selected.entity_id || "") !== String(selection.entity_id || item.id || "")
    || requireExactHead && (
      String(head.revision ?? "") !== String(selection.revision ?? "")
      || String(head.sequence ?? "") !== String(selection.sequence ?? "")
    )
  ) {
    throw new Error("Read-only content response does not match this exact selection.");
  }
  state.selectionContentSessionId = contentSessionId;
  state.selectionContentView = {
    schema: raw.schema,
    handle,
    run_id: runId,
    content_kind: contentKind,
    selection: {
      kind: String(selected.kind),
      entity_id: String(selected.entity_id),
    },
    display: {
      kind: String(options.displayKind || selected.kind),
      entity_id: String(options.displayId || selected.entity_id),
    },
    context_label: String(options.contextLabel || "Read-only files from the selected Run update"),
    head: {
      revision,
      sequence,
    },
    expires_in_seconds: Math.max(0, Number(raw.expires_in_seconds || 0)),
  };
  state.selectionContentTree = null;
  state.selectionContentPreview = null;
  state.selectionContentError = "";
  renderSelectionContentHost();
  if (contentKind === "tree") {
    await loadSelectionContentTree();
  } else {
    await loadSelectionContentPreview("");
  }
}

async function loadSelectionContentTree(options = {}) {
  const view = state.selectionContentView;
  const contentSessionId = state.selectionContentSessionId;
  if (!view || view.content_kind !== "tree" || !contentSessionId || state.selectionContentLoading) return;
  const append = Boolean(options.append);
  const existing = state.selectionContentTree;
  const pageToken = append && existing && existing.page && existing.page.has_more
    ? String(existing.page.next_page_token || "")
    : "";
  if (append && !pageToken) return;
  const requestSeq = ++state.selectionContentRequestSeq;
  state.selectionContentLoading = true;
  state.selectionContentError = "";
  renderSelectionContentHost();
  try {
    const params = new URLSearchParams({
      content_session_id: contentSessionId,
      limit: String(SELECTION_CONTENT_TREE_PAGE_LIMIT),
    });
    if (pageToken) params.set("page_token", pageToken);
    const payload = await getJson(`/api/content-views/${encodeURIComponent(view.handle)}/tree?${params.toString()}`);
    if (!currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) return;
    const page = normalizeSelectionContentTreePage(payload, view, contentSessionId);
    const entries = append && existing ? [...existing.entries] : [];
    const known = new Set(entries.map((entry) => entry.relative_path));
    page.entries.forEach((entry) => {
      if (!known.has(entry.relative_path) && entries.length < SELECTION_CONTENT_TREE_ENTRY_LIMIT) {
        known.add(entry.relative_path);
        entries.push(entry);
      }
    });
    const limitReached = page.page.has_more && entries.length >= SELECTION_CONTENT_TREE_ENTRY_LIMIT;
    state.selectionContentTree = {
      entries,
      page: {
        count: entries.length,
        has_more: page.page.has_more && !limitReached,
        next_page_token: page.page.next_page_token,
      },
      limit_reached: limitReached,
    };
  } catch (error) {
    if (currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) {
      state.selectionContentError = boundedPublicActionError(error, "The saved file list could not be loaded.");
    }
  } finally {
    if (currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) {
      state.selectionContentLoading = false;
      renderSelectionContentHost();
    }
  }
}

function normalizeSelectionContentTreePage(payload, view, contentSessionId) {
  const echoed = payload && payload.content_view;
  if (
    !payload || payload.schema !== "optpilot.selection-content-tree-page.v1"
    || !echoed || echoed.handle !== view.handle
    || echoed.content_session_id !== contentSessionId
  ) {
    throw new Error("Saved file-list response is incomplete.");
  }
  const entries = [];
  const seen = new Set();
  const rawEntries = Array.isArray(payload.entries) ? payload.entries : [];
  if (rawEntries.length > SELECTION_CONTENT_TREE_PAGE_LIMIT) {
    throw new Error("Saved file list exceeded its page bound.");
  }
  for (const raw of rawEntries) {
    const relativePath = safeSelectionRelativePath(raw && raw.relative_path);
    const rawKind = String(raw && raw.kind || "");
    const kind = rawKind === "file" || rawKind === "blob"
      ? "file"
      : rawKind === "directory" || rawKind === "tree" ? "directory" : "";
    if (!relativePath || !kind || seen.has(relativePath)) {
      throw new Error("Saved file list contains an invalid entry.");
    }
    const sizeBytes = Number(raw && raw.size_bytes);
    seen.add(relativePath);
    entries.push({
      relative_path: relativePath,
      kind,
      size_bytes: Number.isFinite(sizeBytes) && sizeBytes >= 0 ? sizeBytes : null,
    });
  }
  const rawPage = payload.page && typeof payload.page === "object" ? payload.page : {};
  const count = Number(rawPage.count);
  const hasMore = Boolean(rawPage.has_more);
  const nextPageToken = String(rawPage.next_page_token || "");
  if (!Number.isInteger(count) || count !== entries.length) {
    throw new Error("Saved file-list count is invalid.");
  }
  if (hasMore && !nextPageToken) throw new Error("Saved file-list continuation is missing.");
  return {
    entries,
    page: {
      count,
      has_more: hasMore,
      next_page_token: nextPageToken,
    },
  };
}

async function loadSelectionContentPreview(relativePath, options = {}) {
  const view = state.selectionContentView;
  const contentSessionId = state.selectionContentSessionId;
  if (!view || !contentSessionId || state.selectionContentLoading) return;
  const selectedPath = view.content_kind === "tree" ? safeSelectionRelativePath(relativePath) : "";
  if (view.content_kind === "tree" && !selectedPath) return;
  const append = Boolean(options.append);
  const existing = state.selectionContentPreview;
  const sameContent = existing && existing.relative_path === selectedPath;
  const offset = append && sameContent ? Number(existing.next_offset || 0) : 0;
  const remaining = SELECTION_CONTENT_PREVIEW_LIMIT - offset;
  if (remaining <= 0 || append && (!sameContent || !existing.has_more)) return;
  const requestSeq = ++state.selectionContentRequestSeq;
  state.selectionContentLoading = true;
  state.selectionContentError = "";
  if (!append) state.selectionContentPreview = null;
  renderSelectionContentHost();
  try {
    const rangeLimit = Math.min(SELECTION_CONTENT_PREVIEW_CHUNK_LIMIT, remaining);
    const params = new URLSearchParams({
      content_session_id: contentSessionId,
      offset: String(offset),
      limit: String(rangeLimit),
    });
    if (view.content_kind === "tree") params.set("relative_path", selectedPath);
    const payload = await getJson(`/api/content-views/${encodeURIComponent(view.handle)}/content?${params.toString()}`);
    if (!currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) return;
    const chunk = normalizeSelectionContentPreview(payload, view, selectedPath, offset, rangeLimit);
    if (append && sameContent && (
      chunk.encoding !== existing.encoding
      || chunk.media_type !== existing.media_type
      || chunk.total_size_bytes !== existing.total_size_bytes
    )) {
      throw new Error("Saved file preview changed across ranges.");
    }
    const chunks = append && sameContent ? [...existing.chunks, chunk.chunk] : [chunk.chunk];
    const limitReached = chunk.has_more && chunk.next_offset >= SELECTION_CONTENT_PREVIEW_LIMIT;
    state.selectionContentPreview = {
      relative_path: selectedPath,
      media_type: chunk.media_type,
      encoding: chunk.encoding,
      chunks,
      offset: 0,
      next_offset: chunk.next_offset,
      total_size_bytes: chunk.total_size_bytes,
      has_more: chunk.has_more && !limitReached,
      limit_reached: limitReached,
    };
  } catch (error) {
    if (currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) {
      state.selectionContentError = boundedPublicActionError(error, "The saved file preview could not be loaded.");
    }
  } finally {
    if (currentSelectionContentRequest(requestSeq, view.handle, contentSessionId)) {
      state.selectionContentLoading = false;
      renderSelectionContentHost();
    }
  }
}

function normalizeSelectionContentPreview(payload, view, selectedPath, expectedOffset, expectedLimit) {
  if (!payload || payload.schema !== "optpilot.selection-content-byte-range.v1") {
    throw new Error("Saved file preview response is incomplete.");
  }
  const relativePath = view.content_kind === "tree"
    ? safeSelectionRelativePath(payload.relative_path)
    : "";
  if (relativePath !== selectedPath) throw new Error("Saved file preview changed selection.");
  const encoding = String(payload.encoding || "");
  const offset = Number(payload.offset);
  const nextOffset = Number(payload.next_offset);
  const totalSizeBytes = Number(payload.total_size_bytes);
  if (
    !["utf-8", "base64"].includes(encoding)
    || !Number.isSafeInteger(offset) || offset !== expectedOffset
    || !Number.isSafeInteger(nextOffset) || nextOffset < offset || nextOffset > offset + expectedLimit
    || !Number.isSafeInteger(totalSizeBytes) || totalSizeBytes < nextOffset
    || (Boolean(payload.has_more) && nextOffset === offset)
  ) {
    throw new Error("Saved file preview range is invalid.");
  }
  const value = String(encoding === "utf-8" ? payload.text ?? "" : payload.data ?? "");
  if (encoding === "base64" && !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
    throw new Error("Saved binary file preview encoding is invalid.");
  }
  const maxCharacters = encoding === "utf-8"
    ? SELECTION_CONTENT_PREVIEW_CHUNK_LIMIT * 4
    : Math.ceil(SELECTION_CONTENT_PREVIEW_CHUNK_LIMIT / 3) * 4 + 4;
  if (value.length > maxCharacters) throw new Error("Saved file preview exceeded its bound.");
  return {
    relative_path: relativePath,
    media_type: String(payload.media_type || "application/octet-stream").slice(0, 160),
    encoding,
    offset,
    next_offset: nextOffset,
    total_size_bytes: totalSizeBytes,
    has_more: Boolean(payload.has_more),
    chunk: { offset, value: encoding === "utf-8" ? value : "" },
  };
}

function safeSelectionRelativePath(value) {
  const path = String(value || "");
  if (!path || path.length > 1024 || path.includes("\\") || path.includes("\0") || path.startsWith("/") || /^[a-zA-Z]:/.test(path)) return "";
  const parts = path.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) return "";
  return parts.join("/");
}

function currentSelectionContentRequest(requestSeq, handle, contentSessionId) {
  return requestSeq === state.selectionContentRequestSeq
    && state.selectionContentView && state.selectionContentView.handle === handle
    && state.selectionContentSessionId === contentSessionId;
}

async function closeSelectionContentView(options = {}) {
  const view = state.selectionContentView;
  const contentSessionId = state.selectionContentSessionId;
  if (!view) return;
  state.selectionContentRequestSeq += 1;
  state.selectionContentView = null;
  state.selectionContentTree = null;
  state.selectionContentPreview = null;
  state.selectionContentLoading = false;
  state.selectionContentError = "";
  if (options.render !== false) renderSelectionContentHost();
  if (!contentSessionId) return;
  try {
    await postJson(`/api/content-views/${encodeURIComponent(view.handle)}/close`, {
      schema: "optpilot.selection-content-view-close-request.v1",
      content_session_id: contentSessionId,
    });
  } catch (error) {
    // The local drawer is closed immediately; the server lease is TTL bounded.
    // Forget a rejected/expired session so the next open can mint a fresh one.
    if (state.selectionContentSessionId === contentSessionId) state.selectionContentSessionId = "";
  }
}

function releaseSelectionContentViewOnUnload() {
  const view = state.selectionContentView;
  const contentSessionId = state.selectionContentSessionId;
  if (!view || !contentSessionId) return;
  fetch(`/api/content-views/${encodeURIComponent(view.handle)}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      schema: "optpilot.selection-content-view-close-request.v1",
      content_session_id: contentSessionId,
    }),
    keepalive: true,
  }).catch(() => {});
}

function renderSelectionContentHost() {
  const host = els.selectionContentDrawerHost;
  if (!host) return;
  host.innerHTML = renderSelectionContentDrawer();
  const contentClose = host.querySelector(".selection-content-close");
  if (contentClose) contentClose.addEventListener("click", () => closeSelectionContentView());
  const treeMore = host.querySelector(".selection-content-tree-more");
  if (treeMore) treeMore.addEventListener("click", () => loadSelectionContentTree({ append: true }));
  const previewMore = host.querySelector(".selection-content-preview-more");
  if (previewMore) {
    previewMore.addEventListener("click", () => {
      const preview = state.selectionContentPreview;
      if (preview) loadSelectionContentPreview(preview.relative_path, { append: true });
    });
  }
  host.querySelectorAll("[data-selection-content-path]").forEach((button) => {
    button.addEventListener("click", () => loadSelectionContentPreview(button.dataset.selectionContentPath || ""));
  });
}

function handleSelectionContentKeydown(event) {
  if (event.key !== "Escape" || !state.selectionContentView) return;
  event.preventDefault();
  closeSelectionContentView();
}

function renderSelectionContentDrawer() {
  const view = state.selectionContentView;
  if (!view) return "";
  const selection = view.display || view.selection || {};
  const tree = state.selectionContentTree;
  const preview = state.selectionContentPreview;
  return `
    <aside class="selection-content-drawer" role="dialog" aria-modal="false" aria-labelledby="selection-content-title">
      <header class="selection-content-heading">
        <div class="selection-content-heading-copy">
          <span class="selection-content-mode">Read-only result</span>
          <h2 id="selection-content-title" title="${escapeHtml(selection.entity_id || "Saved files")}">${escapeHtml(selection.entity_id || "Saved files")}</h2>
          <small>${escapeHtml(view.context_label || "Read-only files")}</small>
        </div>
        <button class="ghost-button compact-action selection-content-close" type="button" aria-label="Close read-only files">Close</button>
      </header>
      ${state.selectionContentError ? `<div class="selection-content-error" role="alert">${escapeHtml(state.selectionContentError)}</div>` : ""}
      <div class="selection-content-layout ${view.content_kind === "blob" ? "blob-content" : ""}">
        ${view.content_kind === "tree" ? renderSelectionContentTree(tree, preview) : ""}
        ${renderSelectionContentPreview(view, preview)}
      </div>
      ${state.selectionContentLoading ? `<div class="selection-content-loading" role="status">Loading read-only result files…</div>` : ""}
    </aside>
  `;
}

function renderSelectionContentTree(tree, preview) {
  const entries = tree && Array.isArray(tree.entries) ? tree.entries : [];
  return `
    <section class="selection-content-tree" aria-label="Saved result file list">
      <div class="selection-content-section-heading">
        <div>
          <strong>Files</strong>
          <small>Select a file to preview</small>
        </div>
        <span>${escapeHtml(entries.length)} shown</span>
      </div>
      <div class="selection-content-tree-list">
        ${entries.map((entry) => {
          const parts = entry.relative_path.split("/");
          const label = parts[parts.length - 1];
          const parent = parts.slice(0, -1).join("/");
          const indent = Math.min(Math.max(parts.length - 1, 0), 3) * 10;
          const selected = preview && preview.relative_path === entry.relative_path;
          return entry.kind === "file" ? `
            <button class="selection-content-entry ${selected ? "selected" : ""}" style="--selection-indent: ${indent}px" type="button" data-selection-content-path="${escapeHtml(entry.relative_path)}" title="${escapeHtml(entry.relative_path)}" ${selected ? 'aria-current="true"' : ""}>
              <span class="selection-content-entry-icon" aria-hidden="true">▤</span>
              <span class="selection-content-entry-copy">
                <strong>${escapeHtml(label)}</strong>
                <span class="selection-content-entry-meta">${parent ? `<small>${escapeHtml(parent)}</small>` : "<small>Root</small>"}<small class="selection-content-entry-size">${entry.size_bytes == null ? "" : escapeHtml(formatBytes(entry.size_bytes))}</small></span>
              </span>
            </button>
          ` : `
            <div class="selection-content-entry directory" style="--selection-indent: ${indent}px" title="${escapeHtml(entry.relative_path)}">
              <span class="selection-content-entry-icon" aria-hidden="true">▰</span>
              <span class="selection-content-entry-copy">
                <strong>${escapeHtml(label)}</strong>
                <span class="selection-content-entry-meta">${parent ? `<small>${escapeHtml(parent)}</small>` : "<small>Root</small>"}<small class="selection-content-entry-size">Folder</small></span>
              </span>
            </div>
          `;
        }).join("") || `<p class="selection-content-empty">${state.selectionContentLoading ? "Loading files…" : "This saved folder is empty."}</p>`}
      </div>
      ${tree && tree.page && tree.page.has_more ? `<button class="ghost-button compact-action selection-content-tree-more" type="button" ${state.selectionContentLoading ? "disabled" : ""}>Load more files</button>` : ""}
      ${tree && tree.limit_reached ? `<p class="selection-content-bound">File preview limit reached. Further files are not shown.</p>` : ""}
    </section>
  `;
}

function renderSelectionContentPreview(view, preview) {
  if (!preview) {
    return `
      <section class="selection-content-preview" aria-label="Read-only file preview">
        ${selectionContentBreadcrumb(view, "")}
        <div class="selection-content-preview-empty">${state.selectionContentLoading ? "Loading preview…" : view.content_kind === "tree" ? "Choose a file to preview it." : "No preview is available."}</div>
      </section>
    `;
  }
  const chunks = Array.isArray(preview.chunks) ? preview.chunks : [];
  const textContent = preview.encoding === "utf-8" ? chunks.map((chunk) => chunk.value).join("") : "";
  return `
    <section class="selection-content-preview" aria-label="Read-only file preview">
      ${selectionContentBreadcrumb(view, preview.relative_path)}
      <div class="selection-content-preview-meta">
        <span>${escapeHtml(preview.media_type || "application/octet-stream")}</span>
        <span>${escapeHtml(formatBytes(preview.next_offset))} of ${escapeHtml(formatBytes(preview.total_size_bytes))}</span>
      </div>
      ${preview.encoding === "utf-8"
        ? `<pre class="selection-content-preview-body text"><code>${escapeHtml(textContent)}</code></pre>`
        : `<div class="selection-content-preview-body binary">Binary content cannot be previewed here.</div>`}
      <div class="selection-content-preview-footer">
        <span>${preview.encoding === "base64" ? "Binary range metadata only." : "Text decoded as UTF-8."}</span>
        ${preview.has_more ? `<button class="ghost-button compact-action selection-content-preview-more" type="button" ${state.selectionContentLoading ? "disabled" : ""}>Load more</button>` : ""}
      </div>
      ${preview.limit_reached ? `<p class="selection-content-bound">Preview limit reached at ${escapeHtml(formatBytes(SELECTION_CONTENT_PREVIEW_LIMIT))}.</p>` : ""}
    </section>
  `;
}

function selectionContentBreadcrumb(view, relativePath) {
  const parts = relativePath ? relativePath.split("/") : [];
  const rootLabel = view.content_kind === "blob" ? "Blob" : "Root";
  const fileName = parts.length ? parts[parts.length - 1] : "No file selected";
  const parentPath = parts.length > 1 ? `${rootLabel} / ${parts.slice(0, -1).join(" / ")}` : rootLabel;
  return `
    <div class="selection-content-breadcrumb" aria-label="Content path">
      <span class="selection-content-breadcrumb-label">${relativePath ? "File preview" : "Preview"}</span>
      <div>
        <strong title="${escapeHtml(relativePath || rootLabel)}">${escapeHtml(fileName)}</strong>
        <small title="${escapeHtml(parentPath)}">${escapeHtml(parentPath)}</small>
      </div>
    </div>
  `;
}

function bindWorkbenchEntityActions() {
  if (!els.runDetail) return;
  els.runDetail.querySelectorAll("[data-open-candidate-route]").forEach((button) => {
    button.addEventListener("click", () => {
      const candidateId = String(button.dataset.openCandidateRoute || "");
      const runId = selectedCanonicalRunId();
      if (!candidateId || !runId) return;
      state.routedCandidateId = candidateId;
      state.routedCandidateResolution = null;
      state.routedCandidateFocusApplied = "";
      state.activeRunTab = "candidate";
      syncStudioRoute();
      loadRunDetail(runId, {
        keepTab: true,
        skipListRender: true,
        fromRoute: true,
      }).catch(() => {});
    });
  });
  els.runDetail.querySelectorAll("[data-clear-candidate-route]").forEach((button) => {
    button.addEventListener("click", () => {
      const candidateId = state.routedCandidateId;
      state.routedCandidateId = null;
      state.routedCandidateResolution = null;
      state.routedCandidateFocusApplied = "";
      syncStudioRoute();
      renderRunDetail();
      window.requestAnimationFrame(() => {
        const candidate = [...els.runDetail.querySelectorAll("[data-open-candidate-route]")]
          .find((control) => control.dataset.openCandidateRoute === candidateId);
        const target = candidate
          || els.runDetail.querySelector("[data-run-tab='candidate']");
        if (target && typeof target.focus === "function") target.focus();
      });
    });
  });
  els.runDetail.querySelectorAll("[data-open-candidate-shortlist]").forEach((button) => {
    button.addEventListener("click", () => {
      state.routedCandidateId = null;
      state.routedCandidateResolution = null;
      state.routedCandidateFocusApplied = "";
      state.activeRunTab = "review";
      syncStudioRoute();
      renderRunDetail();
    });
  });
  els.runDetail.querySelectorAll("details[data-workbench-selection-id]").forEach((details) => {
    details.addEventListener("toggle", () => {
      const selectionId = details.dataset.workbenchSelectionId;
      if (!selectionId) return;
      if (details.open) {
        state.expandedWorkbenchSelections.add(selectionId);
        if (details.dataset.candidateId) {
          if (details.dataset.candidateId !== state.routedCandidateId) {
            state.routedCandidateResolution = null;
            state.routedCandidateFocusApplied = "";
          }
          state.routedCandidateId = details.dataset.candidateId;
        }
      } else {
        state.expandedWorkbenchSelections.delete(selectionId);
        if (details.dataset.candidateId === state.routedCandidateId) {
          state.routedCandidateId = null;
          state.routedCandidateResolution = null;
          state.routedCandidateFocusApplied = "";
        }
      }
      if (details.dataset.candidateId) syncStudioRoute();
      if (details.open && details.dataset.candidateId) {
        const runId = selectedCanonicalRunId();
        window.requestAnimationFrame(() => {
          if (!runId) return;
          loadRunDetail(runId, {
            keepTab: true,
            skipListRender: true,
            fromRoute: true,
          }).catch(() => {
            renderRunDetail();
          });
        });
      }
    });
  });
  if (state.routedCandidateId) {
    const focusKey = `${selectedCanonicalRunId()}:${state.routedCandidateId}`;
    const focusedBack = els.runDetail.querySelector("[data-clear-candidate-route]");
    const target = [...els.runDetail.querySelectorAll("[data-open-candidate-route]")]
      .find((control) => control.dataset.openCandidateRoute === state.routedCandidateId);
    if (focusedBack && state.routedCandidateFocusApplied !== focusKey) {
      state.routedCandidateFocusApplied = focusKey;
      window.requestAnimationFrame(() => focusedBack.focus());
    } else if (target && state.routedCandidateFocusApplied !== focusKey) {
      state.routedCandidateFocusApplied = focusKey;
      window.requestAnimationFrame(() => target.scrollIntoView({ block: "nearest" }));
    }
  }
  els.runDetail.querySelectorAll("[data-workbench-action]").forEach((button) => {
    button.addEventListener("click", () => performWorkbenchAction(button.dataset.workbenchAction, button.dataset.workbenchSelection));
  });
  els.runDetail.querySelectorAll("[data-try-candidate]").forEach((button) => {
    button.addEventListener("click", () => startCandidateTry(button.dataset.tryCandidate, button));
  });
  els.runDetail.querySelectorAll("[data-retry-candidate-inspection]").forEach((button) => {
    button.addEventListener("click", () => {
      const selectionId = button.dataset.retryCandidateInspection;
      delete state.workbenchActionErrors[workbenchActionKey(selectionId, "inspect")];
      renderRunDetail();
      window.requestAnimationFrame(ensureFocusedCandidateInspection);
    });
  });
  els.runDetail.querySelectorAll("[data-workbench-ask-assistant]").forEach((button) => {
    button.addEventListener("click", () => askAssistantAboutWorkbenchSelection(button.dataset.workbenchAskAssistant));
  });
  els.runDetail.querySelectorAll("[data-candidate-compare]").forEach((button) => {
    button.addEventListener("click", () => chooseCandidateComparison(button.dataset.candidateCompare));
  });
  els.runDetail.querySelectorAll("[data-add-to-review]").forEach((button) => {
    button.addEventListener("click", () => addCandidateToReview(button.dataset.addToReview, {
      updateSavedResult: button.dataset.updateSavedResult === "true",
    }));
  });
  const reviewTitle = els.runDetail.querySelector("[data-review-title]");
  if (reviewTitle) {
    reviewTitle.addEventListener("input", () => updateReviewDraftTitle(reviewTitle.value));
  }
  els.runDetail.querySelectorAll("[data-review-note]").forEach((field) => {
    field.addEventListener("input", () => updateReviewDraftNote(Number(field.dataset.reviewNote), field.value));
  });
  els.runDetail.querySelectorAll("[data-review-move]").forEach((button) => {
    button.addEventListener("click", () => moveReviewDraftItem(Number(button.dataset.reviewIndex), button.dataset.reviewMove));
  });
  els.runDetail.querySelectorAll("[data-review-remove]").forEach((button) => {
    button.addEventListener("click", () => removeReviewDraftItem(Number(button.dataset.reviewRemove)));
  });
  const reviewSave = els.runDetail.querySelector(".review-save");
  if (reviewSave) reviewSave.addEventListener("click", saveReviewDraft);
  const reviewExport = els.runDetail.querySelector(".review-export");
  if (reviewExport) reviewExport.addEventListener("click", exportReviewRevision);
  const reviewDelete = els.runDetail.querySelector(".review-delete");
  if (reviewDelete) reviewDelete.addEventListener("click", deleteReviewCollection);
  const reviewRevision = els.runDetail.querySelector("[data-review-revision]");
  if (reviewRevision) {
    reviewRevision.addEventListener("change", () => openReviewRevision(reviewRevision.value));
  }
  const reviewHistoryMore = els.runDetail.querySelector(".review-history-more");
  if (reviewHistoryMore) reviewHistoryMore.addEventListener("click", loadOlderReviewHistory);
  const reviewHistoryCurrent = els.runDetail.querySelector(".review-history-current");
  if (reviewHistoryCurrent) reviewHistoryCurrent.addEventListener("click", () => openReviewRevision("current"));
  const comparisonClear = els.runDetail.querySelector(".candidate-comparison-clear");
  if (comparisonClear) comparisonClear.addEventListener("click", clearCandidateComparison);
  const comparisonSwap = els.runDetail.querySelector(".candidate-comparison-swap");
  if (comparisonSwap) comparisonSwap.addEventListener("click", swapCandidateComparison);
  els.runDetail.querySelectorAll("[data-candidate-text-diff]").forEach((button) => {
    button.addEventListener("click", () => requestCandidateFileTextDiff(button.dataset.candidateTextDiff));
  });
  els.runDetail.querySelectorAll("[data-preview-profile-selection]").forEach((select) => {
    select.addEventListener("change", () => {
      const selectionId = select.dataset.previewProfileSelection;
      if (selectionId) state.environmentPreviewProfileSelections[selectionId] = select.value;
    });
  });
}

function updateReviewDraftTitle(value) {
  const draft = reviewDraft();
  if (!draft) return;
  draft.title = String(value || "");
  draft.dirty = true;
  const save = els.runDetail && els.runDetail.querySelector(".review-save");
  if (save) save.disabled = state.reviewSavePending;
}

function updateReviewDraftNote(index, value) {
  const draft = reviewDraft();
  if (!draft || !draft.items[index]) return;
  draft.items[index].note = String(value || "");
  draft.dirty = true;
  const save = els.runDetail && els.runDetail.querySelector(".review-save");
  if (save) save.disabled = state.reviewSavePending;
}

function moveReviewDraftItem(index, direction) {
  const draft = reviewDraft();
  if (!draft || !draft.items[index]) return;
  const target = direction === "up" ? index - 1 : index + 1;
  if (target < 0 || target >= draft.items.length) return;
  [draft.items[index], draft.items[target]] = [draft.items[target], draft.items[index]];
  draft.dirty = true;
  renderRunDetail();
}

function removeReviewDraftItem(index) {
  const draft = reviewDraft();
  if (!draft || !draft.items[index]) return;
  draft.items.splice(index, 1);
  draft.dirty = true;
  renderRunDetail();
}

function shortlistCommandDraft(detail = state.selectedRun) {
  const draft = reviewDraft(detail);
  if (!draft) {
    return {
      shortlist_id: null,
      expected_revision: null,
      title: "Shortlist",
      cards: [],
    };
  }
  return {
    shortlist_id: draft.collection_id,
    expected_revision: draft.expected_revision,
    title: draft.title,
    cards: draft.items.map((item) => ({
      selection_digest: item.selection_digest,
      note: item.note,
      inspection_outcomes: item.inspection_outcomes,
    })),
  };
}

function shortlistCommandParameters(overrides = {}) {
  return {
    candidate_id: null,
    note: "",
    operator_job_id: null,
    update_saved_result: false,
    ...overrides,
  };
}

function shortlistMutationRequest(runId, command, presentationSelection, draft, parameters) {
  const intentKey = `${runId}:${command}`;
  const fingerprint = JSON.stringify({ command, presentation_selection: presentationSelection, draft, parameters });
  const existing = state.shortlistRequestIntents[intentKey];
  const requestId = existing && existing.fingerprint === fingerprint && existing.request_id
    ? existing.request_id
    : newRequestId();
  state.shortlistRequestIntents[intentKey] = { fingerprint, request_id: requestId };
  storeValue(STORAGE_KEYS.durableShortlistIntents, JSON.stringify(state.shortlistRequestIntents));
  return {
    intentKey,
    requestId,
    payload: {
      schema: "optpilot.run-shortlist-command.v1",
      request_id: requestId,
      command,
      presentation_selection: presentationSelection,
      draft,
      parameters,
    },
  };
}

function completeShortlistMutationIntent(intentKey, requestId) {
  const current = state.shortlistRequestIntents[intentKey];
  if (!current || current.request_id !== requestId) return;
  delete state.shortlistRequestIntents[intentKey];
  storeValue(STORAGE_KEYS.durableShortlistIntents, JSON.stringify(state.shortlistRequestIntents));
}

async function addCandidateToReview(selectionId, options = {}) {
  const runId = selectedCanonicalRunId();
  const item = currentWorkbenchItem(selectionId);
  const operatorJobId = String(options.operatorJobId || "");
  const updateSavedResult = options.updateSavedResult === true;
  if (!runId) {
    state.reviewSelectionErrors[selectionId] = "This Candidate could not be saved because its source Run is no longer open.";
    renderRunDetail();
    return;
  }
  if (!item || item.kind !== "candidate") {
    state.reviewSelectionErrors[selectionId] = "This Candidate could not be resolved from the current Run. Refresh the Candidate and try again.";
    renderRunDetail();
    return;
  }
  if (
    state.reviewPendingSelectionIds.has(selectionId)
    || operatorJobId && state.reviewPendingOperatorJobIds.has(operatorJobId)
  ) return;
  state.reviewPendingSelectionIds.add(selectionId);
  if (operatorJobId) state.reviewPendingOperatorJobIds.add(operatorJobId);
  delete state.reviewSelectionErrors[selectionId];
  state.expandedWorkbenchSelections.add(selectionId);
  renderRunDetail();
  const intent = shortlistMutationRequest(
    runId,
    "save_candidate",
    item.selection,
    shortlistCommandDraft(),
    shortlistCommandParameters({
      operator_job_id: operatorJobId || null,
      update_saved_result: updateSavedResult,
    }),
  );
  try {
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/shortlist`, intent.payload);
    if (!payload || payload.run_id !== runId || !payload.shortlist || !payload.collection) throw new Error("Shortlist response is incomplete.");
    if (state.selectedRunId === runId && state.selectedRun) {
      state.selectedRun.review_collection = payload.collection;
      state.selectedRun.review_collection_history = payload.history || state.selectedRun.review_collection_history;
      delete state.reviewDrafts[runId];
      delete state.reviewViewedCollections[runId];
    }
    delete state.reviewSelectionErrors[selectionId];
    completeShortlistMutationIntent(intent.intentKey, intent.requestId);
  } catch (error) {
    state.reviewSelectionErrors[selectionId] = boundedPublicActionError(
      error,
      updateSavedResult
        ? "This Candidate's saved result could not be updated."
        : "This candidate could not be saved to the Shortlist.",
    );
  } finally {
    state.reviewPendingSelectionIds.delete(selectionId);
    if (operatorJobId) state.reviewPendingOperatorJobIds.delete(operatorJobId);
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

async function saveReviewDraft() {
  const runId = selectedCanonicalRunId();
  const draft = reviewDraft();
  if (!runId || !draft || !draft.dirty || state.reviewSavePending) return;
  state.reviewSavePending = true;
  state.reviewError = "";
  renderRunDetail();
  const intent = shortlistMutationRequest(
    runId,
    "save_changes",
    null,
    shortlistCommandDraft(),
    shortlistCommandParameters(),
  );
  try {
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/shortlist`, intent.payload);
    if (!payload || payload.run_id !== runId || !payload.shortlist || !payload.collection) throw new Error("Saved Shortlist response is incomplete.");
    if (state.selectedRunId === runId && state.selectedRun) {
      state.selectedRun.review_collection = payload.collection;
      state.selectedRun.review_collection_history = payload.history || state.selectedRun.review_collection_history;
      delete state.reviewDrafts[runId];
      delete state.reviewViewedCollections[runId];
    }
    completeShortlistMutationIntent(intent.intentKey, intent.requestId);
  } catch (error) {
    state.reviewError = boundedPublicActionError(error, "The Shortlist changes could not be saved.");
  } finally {
    state.reviewSavePending = false;
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

async function deleteReviewCollection() {
  const runId = selectedCanonicalRunId();
  const collection = reviewCollection();
  const draft = reviewDraft();
  if (!runId || !collection || state.reviewDeletePending || state.reviewSavePending) return;
  const revisionCount = Number(collection.revision || 0);
  const itemCount = Array.isArray(collection.items) ? collection.items.length : 0;
  const unsavedWarning = draft && draft.dirty
    ? " Unsaved Shortlist edits will also be discarded."
    : "";
  if (!window.confirm(
    `Delete this Shortlist and all ${revisionCount} saved version${revisionCount === 1 ? "" : "s"}? This does not delete the source Run or evidence retained elsewhere.${unsavedWarning}`,
  )) return;
  state.reviewDeletePending = true;
  state.reviewError = "";
  renderRunDetail();
  try {
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/review-collection`, {
      schema: "optpilot.review-collection-command.v3",
      request_id: newRequestId(),
      command: "delete",
      presentation_selection: null,
      draft: {
        collection_id: collection.collection_id,
        expected_revision: collection.revision,
        expected_revision_digest: collection.revision_digest,
        confirmation: "delete_review_collection",
      },
    });
    const deletion = payload && payload.deletion;
    if (
      !payload
      || payload.schema !== "optpilot.review-collection-response.v2"
      || payload.run_id !== runId
      || payload.collection !== null
      || !deletion
      || deletion.schema !== "optpilot.review-collection-deletion.v1"
      || deletion.collection_id !== collection.collection_id
      || deletion.previous_revision_digest !== collection.revision_digest
    ) throw new Error("Shortlist deletion response is incomplete.");
    if (state.selectedRunId === runId && state.selectedRun) {
      state.selectedRun.review_collection = null;
      state.selectedRun.review_collection_history = null;
      delete state.reviewDrafts[runId];
      delete state.reviewViewedCollections[runId];
      state.reviewError = "";
      if (state.activeRunTab === "review") state.activeRunTab = "candidate";
    }
  } catch (error) {
    state.reviewError = boundedPublicActionError(
      error,
      `The Shortlist with ${itemCount} current Candidate${itemCount === 1 ? "" : "s"} could not be deleted.`,
    );
  } finally {
    state.reviewDeletePending = false;
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

async function attachOperatorJobToReview(jobId) {
  const runId = selectedCanonicalRunId();
  const draft = reviewDraft();
  const job = state.operatorJobs.find((value) => value.job_id === jobId) || state.selectedOperatorJob;
  const candidateId = String(job && job.target && job.target.candidate_id || "");
  const item = reviewItemForCandidate(candidateId);
  if (!runId || !draft || !item || !jobId || state.reviewPendingOperatorJobIds.has(jobId)) return;
  state.reviewPendingOperatorJobIds.add(jobId);
  delete state.reviewOperatorJobErrors[jobId];
  renderRunDetail();
  const intent = shortlistMutationRequest(
    runId,
    "attach_inspection",
    null,
    shortlistCommandDraft(),
    shortlistCommandParameters({ candidate_id: candidateId, operator_job_id: jobId }),
  );
  try {
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/shortlist`, intent.payload);
    if (!payload || payload.run_id !== runId || !payload.shortlist || !payload.collection) throw new Error("Saved Shortlist try result is incomplete.");
    if (state.selectedRunId === runId && state.selectedRun) {
      state.selectedRun.review_collection = payload.collection;
      state.selectedRun.review_collection_history = payload.history || state.selectedRun.review_collection_history;
      delete state.reviewDrafts[runId];
      delete state.reviewViewedCollections[runId];
    }
    delete state.reviewOperatorJobErrors[jobId];
    completeShortlistMutationIntent(intent.intentKey, intent.requestId);
  } catch (error) {
    state.reviewOperatorJobErrors[jobId] = boundedPublicActionError(
      error,
      "This try result could not be saved to the Shortlist.",
    );
  } finally {
    state.reviewPendingOperatorJobIds.delete(jobId);
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

async function exportReviewRevision() {
  const runId = selectedCanonicalRunId();
  const collection = displayedReviewCollection();
  if (!runId || !collection) return;
  state.reviewError = "";
  try {
    const payload = await getJson(`/api/runs/${encodeURIComponent(runId)}/review-collection?revision=${encodeURIComponent(collection.revision)}&format=export`);
    const exported = payload && payload.collection;
    if (!exported || exported.revision_digest !== collection.revision_digest) throw new Error("Decision export does not match the selected revision.");
    const blob = new Blob([`${JSON.stringify(exported, null, 2)}\n`], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `optpilot-shortlist-${collection.collection_id}-v${collection.revision}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    state.reviewError = boundedPublicActionError(error, "This saved Shortlist version could not be exported.");
    renderRunDetail();
  }
}

async function openReviewRevision(value) {
  const runId = selectedCanonicalRunId();
  const current = reviewCollection();
  if (!runId || !current) return;
  if (value === "current" || Number(value) === Number(current.revision)) {
    delete state.reviewViewedCollections[runId];
    state.reviewError = "";
    renderRunDetail();
    return;
  }
  const revision = Number(value);
  if (!Number.isInteger(revision) || revision <= 0 || state.reviewHistoryPending) return;
  state.reviewHistoryPending = true;
  state.reviewError = "";
  renderRunDetail();
  try {
    const payload = await getJson(`/api/runs/${encodeURIComponent(runId)}/review-collection?revision=${encodeURIComponent(revision)}`);
    const collection = payload && payload.collection;
    if (
      !collection
      || payload.run_id !== runId
      || collection.collection_id !== current.collection_id
      || Number(collection.revision) !== revision
    ) throw new Error("Shortlist history does not match the selected saved version.");
    state.reviewViewedCollections[runId] = collection;
  } catch (error) {
    state.reviewError = boundedPublicActionError(error, "That saved Shortlist version could not be opened.");
  } finally {
    state.reviewHistoryPending = false;
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

async function loadOlderReviewHistory() {
  const runId = selectedCanonicalRunId();
  const detail = state.selectedRun;
  const history = reviewCollectionHistory(detail);
  const before = history && history.page && history.page.next_before_revision;
  if (!runId || !detail || !before || state.reviewHistoryPending) return;
  state.reviewHistoryPending = true;
  state.reviewError = "";
  renderRunDetail();
  try {
    const payload = await getJson(`/api/runs/${encodeURIComponent(runId)}/review-collection?before_revision=${encodeURIComponent(before)}`);
    const older = payload && payload.history;
    if (!older || payload.run_id !== runId || older.collection_id !== history.collection_id) {
      throw new Error("Older Shortlist history does not match this Shortlist.");
    }
    const combined = [...(history.items || []), ...(older.items || [])];
    const unique = new Map(combined.map((item) => [Number(item.revision), item]));
    detail.review_collection_history = {
      ...history,
      items: [...unique.values()].sort((left, right) => Number(right.revision) - Number(left.revision)),
      page: older.page,
    };
  } catch (error) {
    state.reviewError = boundedPublicActionError(error, "Older Shortlist history could not be loaded.");
  } finally {
    state.reviewHistoryPending = false;
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

function entityStateTags(data) {
  return [data.state, data.outcome, data.format, data.kind]
    .filter((value, index, values) => value != null && value !== "" && values.indexOf(value) === index)
    .slice(0, 3)
    .map(String);
}

function renderCorrelation(correlation) {
  const selection = correlation && correlation.selection || {};
  const relation = correlation && correlation.relation || selection.kind || "related";
  return `<span class="tag" title="${escapeHtml(selection.entity_id || "")}">${escapeHtml(relation)}: ${escapeHtml(selection.entity_id || "-")}</span>`;
}

function fieldLabel(value) {
  return String(value || "")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function renderRunTimeline(detail) {
  const capability = detail.workbench.capabilities && detail.workbench.capabilities.timeline || {};
  if (!capability.supported || !capability.eligible) {
    return emptyInline("Event history is unavailable for this Run.");
  }
  const timeline = detail.timeline;
  if (!timeline || !Array.isArray(timeline.items)) {
    return emptyInline("Event history is available, but no events were returned.");
  }
  const paging = timeline.page || {};
  return `
    <div class="workbench-page-heading">
      <div>
        <h3>Event history</h3>
        <p>Events recorded while this Run progressed.</p>
      </div>
      <span class="tag">${escapeHtml(timeline.items.length)} shown</span>
    </div>
    <div class="workbench-entity-list timeline-page-list">
      ${timeline.items.map(renderRunTimelineEvent).join("") || emptyInline("No events are available for this Run update.")}
    </div>
    ${paging.has_more ? `<button class="ghost-button run-page-more" data-run-page-more="timeline" type="button" ${state.runPageLoadingKind === "timeline" ? "disabled" : ""}>${state.runPageLoadingKind === "timeline" ? "Loading…" : "Load more"}</button>` : ""}
  `;
}

function renderRunTimelineEvent(event) {
  const stateValue = event.outcome || event.state || event.phase || "event";
  const correlations = [
    ["candidate", event.candidate_id],
    ["trial", event.logical_trial_id],
    ["attempt", event.attempt_id],
  ].filter(([, value]) => value);
  return `
    <details class="workbench-entity timeline-event">
      <summary>
        <span class="workbench-entity-title">
          <span class="timeline-sequence">#${escapeHtml(event.sequence)}</span>
          <strong>${escapeHtml(event.event || "event")}</strong>
        </span>
        <span class="tag-row">
          <span class="tag">${escapeHtml(event.producer || "unknown producer")}</span>
          ${statusPill(stateValue)}
        </span>
      </summary>
      <div class="workbench-entity-body">
        ${correlations.length ? `<div class="tag-row">${correlations.map(([relation, id]) => `<span class="tag" title="${escapeHtml(id)}">${escapeHtml(relation)}: ${escapeHtml(id)}</span>`).join("")}</div>` : ""}
        <dl class="workbench-data-grid">
          ${[
            ["Event id", event.event_id],
            ["Created", formatRealmTime(event.created_at) || event.created_at],
            ["Phase", event.phase],
            ["State", event.state],
            ["Outcome", event.outcome],
            ["Code", event.code],
            ["Terminal", event.terminal ? "yes" : "no"],
            ["Attempt index", event.attempt_index],
            ["Run revision", event.run_revision],
            ["Payload digest", event.payload_digest],
          ].map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${formatCell(value)}</dd></div>`).join("")}
        </dl>
      </div>
    </details>
  `;
}

async function loadMoreRunPage(kind) {
  const detail = state.selectedRun;
  const runId = detail && detail.run && canonicalRunId(detail.run);
  const head = detail && detail.workbench && detail.workbench.head;
  if (!runId || !head || state.runPageLoadingKind) return;
  const existing = kind === "timeline" ? detail.timeline : workbenchPage(detail, kind);
  const paging = existing && existing.page || {};
  if (!paging.has_more) return;
  const requestSeq = ++state.runPageRequestSeq;
  state.runPageLoadingKind = kind;
  renderRunDetail();
  try {
    let url;
    if (kind === "timeline") {
      const params = new URLSearchParams({
        revision: String(head.revision),
        head_sequence: String(head.sequence),
        after_sequence: String(paging.next_after_sequence),
        limit: "50",
      });
      url = `/api/runs/${encodeURIComponent(runId)}/timeline?${params.toString()}`;
    } else {
      const params = new URLSearchParams({
        page_token: String(paging.next_page_token || ""),
        limit: "50",
      });
      url = `/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(kind)}?${params.toString()}`;
    }
    const incoming = await getJson(url);
    if (requestSeq !== state.runPageRequestSeq || state.selectedRunId !== runId) return;
    if (!sameRunHead(incoming.head, head)) {
      await loadRunDetail(runId, { keepTab: true, skipListRender: true });
      return;
    }
    if (kind === "timeline") {
      state.selectedRun.timeline = mergeBoundedPage(existing, incoming, (item) => String(item.sequence));
    } else {
      state.selectedRun.pages[kind] = mergeBoundedPage(existing, incoming, (item) => item.id);
    }
  } catch (error) {
    // A continuation is exact-head fenced. Refresh instead of mixing old and new pages.
    if (state.selectedRunId === runId) {
      await loadRunDetail(runId, { keepTab: true, skipListRender: true }).catch(() => {});
    }
  } finally {
    if (requestSeq === state.runPageRequestSeq) state.runPageLoadingKind = null;
    if (state.selectedRunId === runId) renderRunDetail();
  }
}

function mergeBoundedPage(existing, incoming, identity) {
  const items = [];
  const seen = new Set();
  [...(existing.items || []), ...(incoming.items || [])].forEach((item) => {
    const key = identity(item);
    if (seen.has(key)) return;
    seen.add(key);
    items.push(item);
  });
  return {
    ...incoming,
    items,
    page: { ...(incoming.page || {}), count: items.length },
  };
}

function formatRealmTime(value) {
  const numeric = Number(value);
  const milliseconds = Number.isFinite(numeric)
    ? numeric < 1e12 ? numeric * 1000 : numeric
    : Date.parse(value);
  if (!Number.isFinite(milliseconds)) return "";
  return new Date(milliseconds).toLocaleString();
}

async function openCatalogEditableWorkspace(component) {
  const capability = componentEditableWorkspaceCapability(component);
  const workspaceId = String(capability.workspace_id || "");
  if (workspaceId && state.sessions.some((session) => session.id === workspaceId)) {
    await selectSession(workspaceId);
    return;
  }
  await openComponentSession(component, "edit");
}

function existingCatalogSourceSession(component) {
  return catalogSourceSessionByKey(componentLaunchKey(component));
}

function catalogSourceSessionByKey(key) {
  const exactKey = String(key || "");
  if (!exactKey) return null;
  return state.sessions.find((session) => (
    isCatalogSourceView(session)
    && String(session.catalogComponentKey || "") === exactKey
  )) || null;
}

function showCatalogSourceSession(session, workbenchMode = "code") {
  if (!isCatalogSourceView(session)) return null;
  state.selectedComponentKey = String(session.catalogComponentKey || "");
  state.selectedSessionId = session.id;
  state.selectedFileKey = firstFileKey(session);
  state.workbenchMode = workbenchMode === "preview" ? "preview" : "code";
  setView("workspace", { allowSupportView: true });
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  return session;
}

async function openComponentSession(component, mode, options = {}) {
  const requestKey = componentLaunchKey(component);
  const initialWorkbenchMode = mode === "edit"
    ? "code"
    : options.workbenchMode === "preview" ? "preview" : "code";
  if (mode !== "edit") {
    rememberCatalogSourceComponent(component);
    const existing = existingCatalogSourceSession(component);
    if (existing) {
      delete state.catalogComponentActions[requestKey];
      return showCatalogSourceSession(existing, initialWorkbenchMode);
    }
  }
  const currentAction = state.catalogComponentActions[requestKey];
  if (currentAction && currentAction.pending) return null;
  state.catalogComponentActions[requestKey] = {
    mode,
    pending: true,
    error: "",
  };
  renderComponentDetail();
  try {
    const action = mode === "edit" ? "edit-copy" : "open-workspace";
    const requestPayload = {};
    if (mode === "edit") {
      if (!state.catalogWorkspaceRequestIds[requestKey]) {
        state.catalogWorkspaceRequestIds[requestKey] = newRequestId();
      }
      requestPayload.request_id = state.catalogWorkspaceRequestIds[requestKey];
    }
    const payload = await postJson(`/api/catalog/${encodeURIComponent(component.kind)}/${encodeURIComponent(component.entry.uid)}/${action}`, requestPayload);
    if (payload.workspace) {
      delete state.catalogWorkspaceRequestIds[requestKey];
      const session = mergeUiWorkspace(payload.workspace);
      if (mode !== "edit") {
        session.catalogComponentKey = requestKey;
        rememberCatalogSourceComponent({
          ...component,
          entry: {
            ...component.entry,
            interface: session.interface || component.entry && component.entry.interface || {},
          },
        });
      }
      state.selectedComponentKey = component.key;
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      state.workbenchMode = initialWorkbenchMode;
      delete state.catalogComponentActions[requestKey];
      setView("workspace", { allowSupportView: mode !== "edit" });
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      return session;
    }
    throw new Error(mode === "edit"
      ? "Studio did not return an editable Workspace."
      : "Studio did not return a read-only source view.");
  } catch (error) {
    state.catalogComponentActions[requestKey] = {
      mode,
      pending: false,
      error: boundedPublicActionError(
        error,
        mode === "edit"
          ? "This editable Workspace could not be opened."
          : "This read-only source could not be opened.",
      ),
    };
    renderComponentDetail();
    return null;
  }
}

async function openComponentInterface(component) {
  const session = await openComponentSession(component, "inspect", { workbenchMode: "preview" });
  if (!session) return;
  const launch = state.interfaceLaunch;
  if (
    launch
    && launch.key === componentLaunchKey(component)
    && !["failed", "stopped"].includes(String(launch.status || ""))
  ) {
    session.catalogLaunchId = String(launch.launch_id || "");
    renderWorkspace();
    return;
  }
  await launchComponentInterface(component);
}

async function resumeStoredInterfaceLaunch() {
  const stored = state.storedInterfaceLaunch;
  const launchId = String(stored && stored.launch_id || "");
  const launchKey = String(stored && stored.key || "");
  if (!launchId || !launchKey || state.interfaceLaunch) return;
  state.interfaceLaunch = { ...stored };
  try {
    const payload = await getJson(`/api/interface-launches/${encodeURIComponent(launchId)}`);
    const launch = payload.launch && typeof payload.launch === "object" ? payload.launch : {};
    state.interfaceLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey);
    if (state.interfaceLaunch.status === "stopped") {
      closeInterfaceStopConfirmation();
      state.interfaceLaunch = null;
      resetActiveInterfaceReturnState();
      persistActiveInterfaceLaunch(null);
      if (launch.recovered) {
        // Retired-launch reconciliation may have restored a missing local
        // checkout/index entry after the initial page load.
        await loadUiWorkspaces();
        rebuildDerivedState();
      }
      renderAll();
      return;
    }
    if (state.interfaceLaunch.status === "failed") {
      closeInterfaceStopConfirmation();
      persistActiveInterfaceLaunch(null);
      renderAll();
      return;
    }
    renderInterfaceLaunchSurface(state.interfaceLaunch);
    if (state.interfaceLaunch.launch_scope === "workspace-transient") {
      pollWorkspaceInterfaceLaunch(launchKey, launchId).catch(() => {
        if (state.interfaceLaunch && state.interfaceLaunch.launch_id === launchId) {
          state.interfaceLaunch = {
            ...state.interfaceLaunch,
            status: "failed",
            error: state.interfaceLaunch.error || "This interface launch could not be restored.",
          };
          persistActiveInterfaceLaunch(null);
          renderWorkspace();
        }
      });
    } else {
      pollComponentInterfaceLaunch(launchKey, launchId).catch(() => {
        if (state.interfaceLaunch && state.interfaceLaunch.launch_id === launchId) {
          state.interfaceLaunch = {
            ...state.interfaceLaunch,
            status: "failed",
            error: state.interfaceLaunch.error || "This interface launch could not be restored.",
          };
          persistActiveInterfaceLaunch(null);
          renderInterfaceLaunchSurface(state.interfaceLaunch);
        }
      });
    }
  } catch (error) {
    // A Studio restart can retire the transient launch. Its saved Workspace
    // remains discoverable through the durable server-side action receipt.
    if (state.interfaceLaunch && state.interfaceLaunch.launch_id === launchId) {
      state.interfaceLaunch = {
        ...state.interfaceLaunch,
        status: "failed",
        error: boundedPublicActionError(error, "This interface launch could not be restored."),
      };
      persistActiveInterfaceLaunch(null);
      renderAll();
    }
  }
}

async function launchComponentInterface(component) {
  const launchKey = componentLaunchKey(component);
  const previousLaunch = state.interfaceLaunch;
  const retryingFailedLaunch = Boolean(
    previousLaunch
    && previousLaunch.status === "failed",
  );
  if (previousLaunch && !retryingFailedLaunch) return;
  if (retryingFailedLaunch) {
    state.interfaceLaunch = null;
    persistActiveInterfaceLaunch(null);
  }
  const profile = componentSelectedInterfaceProfile(component);
  if (!profile) return;
  const capability = componentInterfaceLaunchCapability(component, profile);
  if (capability.eligible !== true) return;
  rememberCatalogSourceComponent(component);
  resetActiveInterfaceReturnState();
  state.interfaceLaunch = {
    key: launchKey,
    label: profile.label || profile.id,
    port: profile.presentation.port,
    profile_id: profile.id,
    startedAt: Date.now(),
    status: "queued",
    error: "",
  };
  state.workbenchMode = isCatalogSourceView() ? "preview" : state.workbenchMode;
  renderInterfaceLaunchSurface(state.interfaceLaunch);
  try {
    const payload = await postJson(`/api/catalog/${encodeURIComponent(component.kind)}/${encodeURIComponent(component.entry.uid)}/launch-interface-job`, {
      profile_id: profile.id,
    });
    const launch = payload.launch || {};
    state.interfaceLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey);
    const sourceSession = catalogSourceSessionByKey(launchKey);
    if (sourceSession && state.interfaceLaunch.launch_id) {
      sourceSession.catalogLaunchId = String(state.interfaceLaunch.launch_id);
    }
    renderInterfaceLaunchSurface(state.interfaceLaunch);
    await pollComponentInterfaceLaunch(launchKey, launch.launch_id);
  } catch (error) {
    if (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
      state.interfaceLaunch = {
        ...state.interfaceLaunch,
        status: "failed",
        error: boundedPublicActionError(error, "This interface could not be started."),
      };
      persistActiveInterfaceLaunch(null);
      renderInterfaceLaunchSurface(state.interfaceLaunch);
    }
  }
}

async function pollComponentInterfaceLaunch(launchKey, launchId) {
  if (!launchId) throw new Error("Interface launch did not return a launch id.");
  let readyObserved = false;
  while (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
    let payload;
    try {
      payload = await getJson(`/api/interface-launches/${encodeURIComponent(launchId)}`);
    } catch (error) {
      if (!readyObserved) throw error;
      await sleep(1000);
      continue;
    }
    const launch = payload.launch || {};
    if (!state.interfaceLaunch || state.interfaceLaunch.key !== launchKey || state.interfaceLaunch.launch_id !== launchId) return;
    state.interfaceLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey);
    if (launch.status === "ready") {
      const firstReady = !readyObserved;
      readyObserved = true;
      const result = launch.result || {};
      const previewUrl = result.preview && result.preview.preview_url;
      if (!previewUrl) throw new Error("Interface launch completed without a Preview URL.");
      if (firstReady) renderInterfaceLaunchSurface(state.interfaceLaunch);
      else updateInterfaceOutputPanel(state.interfaceLaunch);
      await sleep(1000);
      continue;
    }
    renderInterfaceLaunchSurface(state.interfaceLaunch);
    if (launch.status === "stopped") {
      closeInterfaceStopConfirmation();
      state.interfaceLaunch = null;
      resetActiveInterfaceReturnState();
      persistActiveInterfaceLaunch(null);
      renderInterfaceLaunchSurface({ ...launch, key: launchKey });
      return;
    }
    if (launch.status === "failed") {
      closeInterfaceStopConfirmation();
      throw new Error(launch.error || "Interface launch failed.");
    }
    await sleep(1000);
  }
}

async function loadInterfaceOutputTreeChoices(form) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  const select = form && form.querySelector('select[name="path"]');
  const capture = form && form.querySelector(".interface-output-tree-capture");
  const refresh = form && form.querySelector(".interface-output-tree-refresh");
  const errorElement = form && form.querySelector(".interface-output-picker-error");
  if (!launchId || !select || !capture) return;
  const previous = select.value;
  select.disabled = true;
  capture.disabled = true;
  if (refresh) refresh.disabled = true;
  if (errorElement) errorElement.textContent = "";
  try {
    const payload = await getJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/tree-choices`,
    );
    const action = payload.action && typeof payload.action === "object" ? payload.action : {};
    if (!action.eligible) throw new Error(action.reason || action.code || "Output folder selection is unavailable.");
    const paths = Array.isArray(payload.paths) ? payload.paths.map((path) => String(path)) : [];
    if (!paths.length) throw new Error("The launch output root is unavailable.");
    select.innerHTML = paths.map((path) => {
      const label = path === "." ? "Whole output folder" : path;
      return `<option value="${escapeHtml(path)}">${escapeHtml(label)}</option>`;
    }).join("");
    if (paths.includes(previous)) select.value = previous;
    select.disabled = false;
    capture.disabled = false;
  } catch (error) {
    select.innerHTML = `<option value=".">Output folders unavailable</option>`;
    if (errorElement) {
      errorElement.textContent = boundedPublicActionError(
        error,
        "Output folders could not be listed.",
      );
    }
  } finally {
    if (refresh) refresh.disabled = false;
  }
}

async function captureInterfaceOutputTree(form) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  const select = form && form.querySelector('select[name="path"]');
  const labelInput = form && form.querySelector('input[name="label"]');
  const capture = form && form.querySelector(".interface-output-tree-capture");
  const refresh = form && form.querySelector(".interface-output-tree-refresh");
  const errorElement = form && form.querySelector(".interface-output-picker-error");
  const path = select && select.value;
  const label = labelInput && labelInput.value.trim();
  if (!launchId || !path || !label || !capture) return;
  capture.disabled = true;
  if (refresh) refresh.disabled = true;
  if (errorElement) errorElement.textContent = "";
  try {
    const payload = await postJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/capture-tree`,
      { label, path },
    );
    const returnedLaunch = payload.launch && typeof payload.launch === "object" ? payload.launch : {};
    const launchKey = state.interfaceLaunch && state.interfaceLaunch.key || "";
    state.interfaceLaunch = mergeInterfaceLaunchPayload(
      state.interfaceLaunch,
      returnedLaunch,
      launchKey,
    );
    updateInterfaceOutputPanel(state.interfaceLaunch);
  } catch (error) {
    if (errorElement) {
      errorElement.textContent = boundedPublicActionError(
        error,
        "This output folder could not be captured.",
      );
    }
    capture.disabled = false;
  } finally {
    if (refresh) refresh.disabled = false;
  }
}

function interfaceContentContextId(launchId) {
  return `interface:${String(launchId || "")}`;
}

async function viewInterfaceOutput(outputId) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  const output = launch && launch.result && Array.isArray(launch.result.outputs)
    ? launch.result.outputs.find((item) => String(item && item.id || "") === String(outputId || ""))
    : null;
  const viewAction = output && output.actions && output.actions.view_read_only;
  if (!launchId || !outputId || !viewAction || !viewAction.eligible) return;
  if (!updateInterfaceOutput(outputId, { view_pending: true, view_error: "" })) return;
  try {
    if (state.selectionContentView) {
      await closeSelectionContentView({ silent: true });
    }
    const payload = await postJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/${encodeURIComponent(outputId)}/view`,
      {
        schema: "optpilot.interface-output-content-view-request.v1",
        content_session_id: state.selectionContentSessionId || null,
      },
    );
    await openSelectionContentView(
      payload,
      {
        kind: "artifact",
        id: outputId,
        selection: { kind: "artifact", entity_id: outputId },
      },
      interfaceContentContextId(launchId),
      {
        requireExactHead: false,
        displayKind: "Result",
        displayId: String(output.label || outputId),
        contextLabel: "Read-only files reported by this interface",
      },
    );
    updateInterfaceOutput(outputId, { view_pending: false, view_error: "" });
  } catch (error) {
    updateInterfaceOutput(outputId, {
      view_pending: false,
      view_error: boundedPublicActionError(error, "This interface result could not be opened."),
    });
  }
}

async function retryInterfaceOutput(outputId) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  if (!launchId || !outputId || !updateInterfaceOutput(outputId, { retry_pending: true, retry_error: "" })) return;
  try {
    const payload = await postJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/${encodeURIComponent(outputId)}/retry`,
      {},
    );
    const returned = payload.output && typeof payload.output === "object" ? payload.output : {};
    updateInterfaceOutput(outputId, {
      ...returned,
      status: returned.status || "sealing",
      error_code: returned.error_code || "",
      retry_pending: false,
      retry_error: "",
    });
  } catch (error) {
    updateInterfaceOutput(outputId, {
      retry_pending: false,
      retry_error: boundedPublicActionError(error, "This output capture could not be retried."),
    });
  }
}

async function runInterfaceOutputAction(outputId, actionId, argumentsList = []) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  if (!launchId || !outputId || !actionId || !updateInterfaceOutput(outputId, {
    execute_pending_action_id: actionId,
    execute_error: "",
  })) return false;
  try {
    const payload = await postJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/${encodeURIComponent(outputId)}/actions/${encodeURIComponent(actionId)}/run`,
      {
        schema_version: "optpilot.interface-output-action-run-request.v1",
        request_id: newRequestId(),
        arguments: Array.isArray(argumentsList) ? argumentsList : [],
      },
    );
    const returnedLaunch = payload.launch && typeof payload.launch === "object"
      ? payload.launch
      : {};
    state.interfaceOutputArgumentDrafts.delete(
      interfaceOutputArgumentDraftKey(outputId, actionId),
    );
    const launchKey = state.interfaceLaunch && state.interfaceLaunch.key || "";
    state.interfaceLaunch = mergeInterfaceLaunchPayload(
      state.interfaceLaunch,
      returnedLaunch,
      launchKey,
    );
    const returnedOutput = payload.output && typeof payload.output === "object"
      ? payload.output
      : null;
    if (returnedOutput) {
      updateInterfaceOutput(outputId, {
        ...returnedOutput,
        execute_pending_action_id: "",
        execute_error: "",
      });
    } else {
      updateInterfaceOutput(outputId, {
        execute_pending_action_id: "",
        execute_error: "",
      });
    }
    return true;
  } catch (error) {
    updateInterfaceOutput(outputId, {
      execute_pending_action_id: "",
      execute_error: boundedPublicActionError(
        error,
        "This output could not be run.",
      ),
    });
    return false;
  }
}

async function keepInterfaceOutput(outputId) {
  const launch = state.interfaceLaunch;
  const launchId = launch && launch.launch_id;
  const output = launch && launch.result && Array.isArray(launch.result.outputs)
    ? launch.result.outputs.find((item) => item && item.id === outputId)
    : null;
  const requestId = output && output.keep_request_id || newRequestId();
  if (!launchId || !outputId || !updateInterfaceOutput(outputId, {
    keep_pending: true,
    keep_error: "",
    keep_request_id: requestId,
  })) return false;
  try {
    const payload = await postJson(
      `/api/interface-launches/${encodeURIComponent(launchId)}/outputs/${encodeURIComponent(outputId)}/keep`,
      { request_id: requestId },
    );
    if (!payload.workspace || typeof payload.workspace !== "object") {
      throw new Error("Keep response did not include an editable workspace.");
    }
    const workspace = mergeUiWorkspace(payload.workspace);
    if (!workspace) throw new Error("Keep response did not include an editable workspace.");
    renderWorkspace();
    updateInterfaceOutput(outputId, {
      keep_pending: false,
      keep_error: "",
      keep_state: "saved",
      kept_workspace_id: workspace.id,
      kept_workspace_title: workspace.title,
    });
    return true;
  } catch (error) {
    updateInterfaceOutput(outputId, {
      keep_pending: false,
      keep_error: boundedPublicActionError(error, "This output could not be saved as a Workspace."),
    });
    return false;
  }
}

async function stopComponentInterface(component) {
  await stopInterfaceLaunch(componentLaunchKey(component));
}

function unsavedReadyInterfaceOutputs(launch) {
  const outputs = launch && launch.result && Array.isArray(launch.result.outputs) ? launch.result.outputs : [];
  return outputs.filter((output) => output
    && output.status === "ready"
    && output.actions && output.actions.keep_as_workspace
    && output.actions.keep_as_workspace.eligible
    && !output.kept_workspace_id);
}

function renderInterfaceStopConfirmation() {
  const pending = state.pendingInterfaceStop;
  const launch = state.interfaceLaunch;
  if (!pending || !launch || launch.launch_id !== pending.launchId || !els.interfaceStopModal) {
    closeInterfaceStopConfirmation();
    return;
  }
  const outputs = unsavedReadyInterfaceOutputs(launch);
  if (!outputs.length) {
    closeInterfaceStopConfirmation();
    performInterfaceStop(pending.launchKey);
    return;
  }
  const next = outputs[0];
  els.interfaceStopBody.innerHTML = `
    <p>${outputs.length === 1
      ? "This output folder is ready but still temporary."
      : `${outputs.length} output folders are ready but still temporary.`}</p>
    <ul class="interface-stop-output-list">
      ${outputs.map((output, index) => `<li${index === 0 ? ' class="next"' : ""}>${escapeHtml(output.label || output.id || "Output")}</li>`).join("")}
    </ul>
    <p>${outputs.length === 1
      ? "Save it as an editable Workspace, or stop and let the temporary output go."
      : `Save them one at a time. The next click saves “${escapeHtml(next.label || next.id || "Output")}”; the interface stops after the last save.`}</p>
  `;
  if (els.interfaceStopError) {
    els.interfaceStopError.textContent = "";
    els.interfaceStopError.hidden = true;
  }
  els.interfaceStopSaveButton.disabled = false;
  els.interfaceStopSaveButton.textContent = "Save as Workspace";
  els.interfaceStopDiscardButton.disabled = false;
  els.interfaceStopCancelButton.disabled = false;
}

function openInterfaceStopConfirmation(launchKey, outputs) {
  const launch = state.interfaceLaunch;
  if (!launch || !launch.launch_id || !outputs.length || !els.interfaceStopModal) return;
  const activeElement = document.activeElement;
  state.interfaceStopReturnFocus = activeElement && activeElement !== document.body
    ? activeElement
    : null;
  state.pendingInterfaceStop = {
    launchKey,
    launchId: launch.launch_id,
  };
  els.interfaceStopModal.hidden = false;
  renderInterfaceStopConfirmation();
  els.interfaceStopSaveButton.focus();
}

function closeInterfaceStopConfirmation(options = {}) {
  const returnFocus = state.interfaceStopReturnFocus;
  const fallbackSelector = state.interfaceLaunch && state.interfaceLaunch.launch_scope === "workspace-transient"
    ? ".workspace-stop-interface"
    : ".component-stop-interface";
  state.pendingInterfaceStop = null;
  state.interfaceStopReturnFocus = null;
  if (els.interfaceStopModal) els.interfaceStopModal.hidden = true;
  if (els.interfaceStopError) {
    els.interfaceStopError.textContent = "";
    els.interfaceStopError.hidden = true;
  }
  if (options.restoreFocus !== false) {
    window.requestAnimationFrame(() => {
      const fallback = document.querySelector(fallbackSelector);
      const target = returnFocus && returnFocus.isConnected ? returnFocus : fallback;
      if (target && typeof target.focus === "function") target.focus();
    });
  }
}

function handleInterfaceStopConfirmationKeydown(event) {
  if (!state.pendingInterfaceStop || !els.interfaceStopModal) return;
  const saving = Boolean(els.interfaceStopSaveButton && els.interfaceStopSaveButton.disabled);
  if (event.key === "Escape" && !saving) {
    event.preventDefault();
    closeInterfaceStopConfirmation();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...els.interfaceStopModal.querySelectorAll(
    "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])",
  )].filter((element) => !element.hidden && !element.closest("[hidden]"));
  if (!focusable.length) {
    event.preventDefault();
    if (els.interfaceStopDialog) els.interfaceStopDialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const activeIsFocusable = focusable.includes(document.activeElement);
  if (event.shiftKey && (document.activeElement === first || !activeIsFocusable)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !activeIsFocusable)) {
    event.preventDefault();
    first.focus();
  }
}

async function savePendingInterfaceOutputAndContinueStop() {
  const pending = state.pendingInterfaceStop;
  const launch = state.interfaceLaunch;
  if (!pending || !launch || launch.launch_id !== pending.launchId) {
    closeInterfaceStopConfirmation();
    return;
  }
  const output = unsavedReadyInterfaceOutputs(launch)[0];
  if (!output) {
    const launchKey = pending.launchKey;
    closeInterfaceStopConfirmation();
    await performInterfaceStop(launchKey);
    return;
  }
  els.interfaceStopSaveButton.disabled = true;
  els.interfaceStopSaveButton.textContent = "Saving…";
  els.interfaceStopDiscardButton.disabled = true;
  els.interfaceStopCancelButton.disabled = true;
  const saved = await keepInterfaceOutput(output.id);
  if (!saved) {
    if (els.interfaceStopError) {
      els.interfaceStopError.textContent = "This output could not be saved. The interface is still running; retry or cancel before stopping.";
      els.interfaceStopError.hidden = false;
    }
    els.interfaceStopSaveButton.disabled = false;
    els.interfaceStopSaveButton.textContent = "Retry save";
    els.interfaceStopDiscardButton.disabled = false;
    els.interfaceStopCancelButton.disabled = false;
    return;
  }
  renderInterfaceStopConfirmation();
}

async function discardPendingInterfaceOutputsAndStop() {
  const pending = state.pendingInterfaceStop;
  if (!pending) return;
  const launchKey = pending.launchKey;
  closeInterfaceStopConfirmation();
  await performInterfaceStop(launchKey);
}

function renderInterfaceLaunchSurface(launch) {
  renderActiveInterfaceIndicator();
  if (launch && launch.launch_scope === "workspace-transient") {
    renderWorkspace();
    return;
  }
  const inspectedComponent = catalogSourceComponent();
  if (
    state.view === "workspace"
    && inspectedComponent
    && launch
    && launch.key === componentLaunchKey(inspectedComponent)
  ) {
    renderWorkspace();
    return;
  }
  if (state.view === "catalog") renderComponentDetail();
}

function clearWorkspaceInterfacePreview(launch, status, message) {
  if (!launch || launch.launch_scope !== "workspace-transient") return;
  const session = workspaceSessionByBackendId(launch.source_workspace_id);
  if (!session) return;
  const preview = currentWorkspacePreview(session);
  preview.url = "";
  preview.status = status;
  preview.message = message;
}

async function stopInterfaceLaunch(launchKey) {
  const launch = state.interfaceLaunch;
  if (!launch || launch.key !== launchKey || !launch.launch_id) return;
  const unsavedReady = unsavedReadyInterfaceOutputs(launch);
  if (unsavedReady.length) {
    openInterfaceStopConfirmation(launchKey, unsavedReady);
    return;
  }
  await performInterfaceStop(launchKey);
}

async function performInterfaceStop(launchKey) {
  const launch = state.interfaceLaunch;
  if (!launch || launch.key !== launchKey || !launch.launch_id) return;
  if (
    state.selectionContentView
    && state.selectionContentView.run_id === interfaceContentContextId(launch.launch_id)
  ) {
    await closeSelectionContentView({ silent: true });
  }
  state.interfaceLaunch = { ...launch, status: "stopping", stop_error: "" };
  renderInterfaceLaunchSurface(state.interfaceLaunch);
  try {
    const payload = await postJson(`/api/interface-launches/${encodeURIComponent(launch.launch_id)}/stop`, {});
    const stoppedLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, payload.launch || { status: "stopped" }, launchKey);
    if (stoppedLaunch.status !== "stopped") {
      state.interfaceLaunch = stoppedLaunch;
      clearWorkspaceInterfacePreview(stoppedLaunch, "error", "Interface stopped, but launch-scoped cleanup is still pending.");
      renderInterfaceLaunchSurface(stoppedLaunch);
      return;
    }
    clearWorkspaceInterfacePreview(stoppedLaunch, "idle", "Interface stopped. Launch it again when you want to inspect this workspace.");
    closeInterfaceStopConfirmation();
    state.interfaceLaunch = null;
    resetActiveInterfaceReturnState();
    persistActiveInterfaceLaunch(null);
    renderInterfaceLaunchSurface(stoppedLaunch);
  } catch (error) {
    state.interfaceLaunch = {
      ...launch,
      stop_error: boundedPublicActionError(error, "This interface could not be stopped."),
    };
    renderInterfaceLaunchSurface(state.interfaceLaunch);
  }
}

async function stopWorkspaceInterface(launchKey) {
  await stopInterfaceLaunch(launchKey);
}

function bindWorkspaceInterfaceLaunchControls(launchState) {
  const root = els.workspaceInterfaceLaunchStatus;
  if (!root) return;
  const stopButton = root.querySelector(".workspace-stop-interface");
  if (stopButton) stopButton.addEventListener("click", () => stopWorkspaceInterface(launchState.key));
  const retryButton = root.querySelector(".workspace-retry-interface");
  if (retryButton) retryButton.addEventListener("click", launchWorkspaceInterface);
  bindInterfaceLaunchDisclosureControls(root);
  bindInterfaceOutputControls(root);
}

function applyWorkspacePreviewPayload(session, previewPayload, interfaceConfig = {}) {
  if (!session || !previewPayload || !previewPayload.preview_url) return;
  const preview = currentWorkspacePreview(session);
  preview.port = Number(previewPayload.port || interfaceConfig.presentation && interfaceConfig.presentation.port || preview.port || 5173);
  preview.url = String(previewPayload.preview_url || "");
  preview.status = "ready";
  const label = interfaceConfig && interfaceConfig.label ? interfaceConfig.label : "interface";
  preview.message = `Previewing ${label} on port ${preview.port} through ${session.title}.`;
  session.timeline.push(["tool", "interface launched", preview.message]);
}

async function createBlankSession() {
  try {
    const title = nextDraftWorkspaceTitle();
    const payload = await postJson("/api/workspaces", {
      title,
      description: "Draft Workspace",
      attached_sessions: [],
    });
    if (payload.workspace) {
      const session = mergeUiWorkspace(payload.workspace);
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      setView("workspace");
      renderWorkspace();
      return;
    }
  } catch (error) {
    state.codeWorkspaceStatus = "failed";
    state.codeWorkspaceMessage = `Workspace create failed: ${String(error.message || error)}`;
    renderWorkspace();
  }
}

function openLocalFolderDialog() {
  if (!els.openLocalFolderModal) return;
  const activeElement = document.activeElement;
  state.localFolderReturnFocus = activeElement && activeElement !== document.body
    ? activeElement
    : null;
  els.openLocalFolderModal.hidden = false;
  if (els.openLocalFolderError) {
    els.openLocalFolderError.hidden = true;
    els.openLocalFolderError.textContent = "";
  }
  window.requestAnimationFrame(() => els.openLocalFolderPath && els.openLocalFolderPath.focus());
}

function closeLocalFolderDialog(options = {}) {
  const returnFocus = state.localFolderReturnFocus;
  state.localFolderReturnFocus = null;
  if (els.openLocalFolderModal) els.openLocalFolderModal.hidden = true;
  if (els.openLocalFolderSubmitButton) {
    els.openLocalFolderSubmitButton.disabled = false;
    els.openLocalFolderSubmitButton.textContent = "Open folder";
  }
  if (options.restoreFocus !== false) {
    window.requestAnimationFrame(() => {
      const target = returnFocus && returnFocus.isConnected
        ? returnFocus
        : els.openLocalFolderButton;
      if (target && typeof target.focus === "function") target.focus();
    });
  }
}

function handleLocalFolderDialogKeydown(event) {
  if (!els.openLocalFolderModal || els.openLocalFolderModal.hidden) return;
  const submitting = Boolean(
    els.openLocalFolderSubmitButton && els.openLocalFolderSubmitButton.disabled,
  );
  if (event.key === "Escape" && !submitting) {
    event.preventDefault();
    closeLocalFolderDialog();
    return;
  }
  trapModalFocus(event, els.openLocalFolderModal, els.openLocalFolderDialog);
}

async function connectLocalFolder() {
  const path = els.openLocalFolderPath && els.openLocalFolderPath.value.trim();
  const title = els.openLocalFolderName && els.openLocalFolderName.value.trim();
  if (!path) {
    if (els.openLocalFolderError) {
      els.openLocalFolderError.textContent = "Enter an existing folder path.";
      els.openLocalFolderError.hidden = false;
    }
    return;
  }
  if (els.openLocalFolderSubmitButton) {
    els.openLocalFolderSubmitButton.disabled = true;
    els.openLocalFolderSubmitButton.textContent = "Opening…";
  }
  try {
    const payload = await postJson("/api/workspaces/connect-local-folder", { path, title });
    const session = mergeUiWorkspace(payload.workspace);
    if (!session) throw new Error("Studio did not return the connected Workspace.");
    state.selectedSessionId = session.id;
    state.selectedFileKey = firstFileKey(session);
    closeLocalFolderDialog({ restoreFocus: false });
    setView("workspace");
    renderWorkspace();
  } catch (error) {
    if (els.openLocalFolderError) {
      els.openLocalFolderError.textContent = String(error.message || error);
      els.openLocalFolderError.hidden = false;
    }
    if (els.openLocalFolderSubmitButton) {
      els.openLocalFolderSubmitButton.disabled = false;
      els.openLocalFolderSubmitButton.textContent = "Open folder";
    }
  }
}

function nextDraftWorkspaceTitle() {
  const titles = new Set(state.sessions.map((session) => String(session.title || "").toLowerCase()));
  for (let index = 1; index < 1000; index += 1) {
    const title = `Draft Workspace ${index}`;
    if (!titles.has(title.toLowerCase())) return title;
  }
  return `Draft Workspace ${Date.now()}`;
}

function createPlanFromPair(pair) {
  if (!pair || !pair.environment || !pair.method) return;
  const plan = planFromPair(pair);
  upsertPlan(plan);
  state.selectedPlanId = plan.id;
  setView("experiments");
}

function createPlanFromCurrentContext() {
  const pair = firstCompatiblePair();
  if (!pair) return;
  const plan = planFromPair(pair);
  upsertPlan(plan);
  state.selectedPlanId = plan.id;
  setView("experiments");
}

async function generatePlanDraft(plan) {
  if (!plan || plan.savePending || plan.launchPending) return;
  const activeLaunch = studyLaunchForPlan(plan);
  if (activeLaunch && !studyLaunchIsTerminal(activeLaunch)) return;
  if (blockUnpublishedStudyAction(plan, "save")) return;
  plan.savePending = true;
  plan.actionError = null;
  renderExperiments();
  try {
    await savePlanDraft(plan, { render: false, persist: true, errorKind: "save" });
  } catch (error) {
    setStudyActionError(
      plan,
      "save",
      "Study could not be saved",
      error && error.message || "This Study draft could not be saved.",
    );
  } finally {
    plan.savePending = false;
    renderExperiments();
  }
}

async function savePlanDraft(plan, options = {}) {
  if (!plan.environment || !plan.method) return;
  if (plan.study) convertSavedPlanToDraft(plan);
  const persist = options.persist !== false;
  const result = await postJson(
    "/api/studies/draft",
    planPayload(plan, { saveAsDraft: persist }),
    { tolerateError: true },
  );
  if (result && result.error) {
    plan.draft = {
      ...(plan.draft || {}),
      error: result.error,
      validation: result.validation || { valid: false, errors: [result.error] },
    };
    plan.status = "review";
    setStudyActionError(
      plan,
      options.errorKind || "save",
      options.errorKind === "launch" ? "Run could not be prepared" : "Study could not be saved",
      result.error,
    );
    if (options.render !== false) renderExperiments();
    return null;
  }
  if (!result || typeof result !== "object") {
    throw new Error("Studio returned an incomplete Study draft response.");
  }
  if (plan.actionError && plan.actionError.kind === (options.errorKind || "save")) {
    plan.actionError = null;
  }
  plan.draft = result;
  if (persist) {
    plan.id = `draft-${result.draft_id}`;
    state.selectedPlanId = plan.id;
    plan.title = plan.name || plan.title;
    plan.source = "Saved draft";
    plan.draftSaveRequestId = null;
    plan.draftActionId = null;
  } else {
    plan.launchPreparationRequestId = null;
  }
  plan.validation = null;
  plan.yaml = result.yaml || plan.yaml;
  plan.status = result.validation && result.validation.valid ? "saved" : "review";
  reconcileStudyDraftAfterSave(result);
  if (options.render !== false) renderExperiments();
  return result;
}

function reconcileStudyDraftAfterSave(draft) {
  window.setTimeout(() => {
    Promise.allSettled([
      loadStudyDrafts(),
      refreshStudyDraftWorkspace(draft),
    ]);
  }, 0);
}

async function refreshStudyDraftWorkspace(draft) {
  if (!draft || !draft.workspace_id) return;
  try {
    const payload = await getJson("/api/workspaces");
    const workspace = (payload.workspaces || []).find((item) => item.id === draft.workspace_id);
    if (workspace) mergeUiWorkspace(workspace);
  } catch (error) {
    // The durable draft is still usable by id; the Workspace list can catch up on refresh.
  }
}

async function launchPlan(plan) {
  if (!plan || plan.savePending || plan.launchPending) return;
  const currentLaunch = studyLaunchForPlan(plan);
  if (currentLaunch && !studyLaunchIsTerminal(currentLaunch)) return;
  const trackedLaunch = state.studyLaunch;
  if (
    trackedLaunch
    && trackedLaunch.planId !== plan.id
    && !studyLaunchIsTerminal(trackedLaunch)
  ) {
    setStudyActionError(
      plan,
      "launch",
      "Another Run is being prepared",
      `Wait for ${trackedLaunch.planTitle || "the other Study"} to finish preparing before launching this Study.`,
    );
    renderExperiments();
    return;
  }
  if (blockUnpublishedStudyAction(plan, "launch")) return;
  const failLaunch = (title, message) => {
    setStudyActionError(plan, "launch", title, message);
    plan.launchPending = false;
    renderExperiments();
  };
  plan.launchPending = true;
  plan.actionError = null;
  renderExperiments();
  const runtimeSetupReason = studyRuntimeSetupReason(plan);
  if (runtimeSetupReason) {
    failLaunch(
      "Run needs setup",
      runtimeSetupReason,
    );
    return;
  }
  const knownCapability = studyLaunchCapability(plan);
  if (knownCapability && knownCapability.eligible !== true) {
    failLaunch(
      "Study launch unavailable",
      publicStudyLaunchReason(knownCapability),
    );
    return;
  }
  const catalogStudyRef = plan.study && plan.study.ref;
  if (!catalogStudyRef && (!hasCurrentWorkspaceStudyDraft(plan.draft) || (plan.draft.validation && !plan.draft.validation.valid))) {
    let saved;
    try {
      saved = await savePlanDraft(plan, {
        render: false,
        persist: false,
        errorKind: "launch",
      });
    } catch (error) {
      failLaunch(
        "Run could not be prepared",
        error && error.message || "The current Study could not be prepared for launch.",
      );
      return;
    }
    if (!saved || saved.validation && !saved.validation.valid) {
      if (!plan.actionError) {
        setStudyActionError(
          plan,
          "launch",
          "Config needs review",
          "The Study was checked, but validation did not pass, so no Run was launched.",
        );
      }
      plan.launchPending = false;
      renderExperiments();
      return;
    }
  }
  const savedCapability = studyLaunchCapability(plan);
  if (savedCapability && savedCapability.eligible !== true) {
    failLaunch(
      "Study launch unavailable",
      publicStudyLaunchReason(savedCapability),
    );
    return;
  }
  if (!catalogStudyRef && !hasCurrentWorkspaceStudyDraft(plan.draft)) {
    failLaunch(
      "Study launch unavailable",
      "Save or prepare this Study before launching it.",
    );
    return;
  }
  const requestId = newRequestId();
  const configuredMethodRequestTimeout = plan.methodRequestTimeoutSeconds;
  const methodRequestTimeoutSeconds = Number(
    configuredMethodRequestTimeout === ""
      || configuredMethodRequestTimeout === null
      || configuredMethodRequestTimeout === undefined
      ? 10
      : configuredMethodRequestTimeout,
  );
  const request = catalogStudyRef
    ? {
        schema: "optpilot.studio-study-launch-request.v1",
        request_id: requestId,
        method_request_timeout_seconds: methodRequestTimeoutSeconds,
        study_ref: catalogStudyRef,
      }
    : {
        schema: "optpilot.studio-study-launch-request.v1",
        request_id: requestId,
        method_request_timeout_seconds: methodRequestTimeoutSeconds,
        workspace_id: plan.draft.workspace_id,
        study_relative_path: plan.draft.study_relative_path,
        expected_workspace_revision: plan.draft.workspace_revision,
      };
  const active = {
    schema: "optpilot.studio-active-study-launch.v1",
    planId: plan.id,
    planTitle: plan.title,
    requestId,
    request,
    launchId: "",
    launch: null,
    stage: "Preparing Run",
    message: "OptPilot is checking this Study and preparing exact Run inputs. You can leave this page while it prepares.",
    status: "preparing",
    preparationAccepted: false,
    startedAt: Date.now(),
    failure: null,
    stopPending: false,
    stopError: "",
  };
  state.studyLaunch = active;
  plan.launchPending = false;
  plan.actionError = null;
  persistActiveStudyLaunch(active);
  const generation = ++state.studyLaunchPollGeneration;
  renderExperiments();
  submitStudyLaunch(active, generation);
}

function persistActiveStudyLaunch(active) {
  if (!active) {
    state.storedStudyLaunch = {};
    storeValue(STORAGE_KEYS.activeStudyLaunch, null);
    return;
  }
  const stored = {
    schema: active.schema,
    planId: active.planId,
    planTitle: active.planTitle,
    requestId: active.requestId,
    request: active.request,
    launchId: active.launchId || active.launch && active.launch.launch_id || "",
    stage: active.stage || "",
    message: active.message || "",
    status: active.status || "",
    elapsedSeconds: active.elapsedSeconds,
    preparationAccepted: Boolean(active.preparationAccepted),
    startedAt: active.startedAt,
    failure: active.failure || null,
    stopRequestId: active.stopRequestId || "",
  };
  state.storedStudyLaunch = stored;
  storeValue(STORAGE_KEYS.activeStudyLaunch, JSON.stringify(stored));
}

function resumeStoredStudyLaunch() {
  const stored = state.storedStudyLaunch;
  if (!stored || stored.schema !== "optpilot.studio-active-study-launch.v1" || !stored.requestId || !stored.request) return;
  if (state.studyLaunch && state.studyLaunch.requestId === stored.requestId) return;
  const active = {
    ...stored,
    launch: null,
    stage: stored.stage || (stored.launchId ? "Restoring launch status" : "Recovering launch request"),
    failure: stored.failure || null,
    stopPending: false,
    stopError: "",
  };
  state.studyLaunch = active;
  const generation = ++state.studyLaunchPollGeneration;
  if (state.view === "experiments") renderExperiments();
  if (studyLaunchIsTerminal(active)) return;
  if (active.launchId) pollStudyLaunch(active, generation, { immediate: true });
  else if (active.preparationAccepted) pollStudyLaunchRequest(active, generation, { immediate: true });
  else submitStudyLaunch(active, generation);
}

function studyLaunchPreparationRequestId(payload) {
  return String(payload && (payload.request_id || payload.requestId) || "");
}

function studyLaunchPreparationState(payload) {
  return String(payload && payload.state || "").toLowerCase();
}

function studyLaunchPreparationLaunch(payload) {
  if (payload && payload.launch && payload.launch.launch_id) return payload.launch;
  const current = payload && payload.current;
  if (current && current.launch && current.launch.launch_id) return current.launch;
  if (current && current.launch_id) return current;
  return null;
}

function applyStudyLaunchPreparationPayload(active, payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("Study launch response is incomplete.");
  }
  const responseRequestId = studyLaunchPreparationRequestId(payload);
  if (responseRequestId && responseRequestId !== active.requestId) {
    throw new Error("Study launch response belongs to a different request.");
  }
  const preparationState = studyLaunchPreparationState(payload);
  const launch = studyLaunchPreparationLaunch(payload);
  const elapsedSeconds = Number(payload.elapsed_seconds);
  if (Number.isFinite(elapsedSeconds)) active.elapsedSeconds = Math.max(0, elapsedSeconds);
  if (payload.stage) active.stage = String(payload.stage);
  if (payload.message) active.message = publicStudyMessage(payload.message);
  if (preparationState) active.status = preparationState;

  if (["failed", "cancelled", "rejected"].includes(preparationState)) {
    const failure = payload.failure && typeof payload.failure === "object" ? payload.failure : {};
    active.failure = {
      code: failure.code || payload.code || "study_launch_rejected",
      message: publicStudyMessage(
        failure.message || payload.error || payload.message || "OptPilot could not prepare this Run."
      ),
    };
    active.stage = payload.stage || "Preparation failed";
    active.preparationAccepted = true;
    return "failed";
  }
  if (launch) {
    active.launch = launch;
    active.launchId = launch.launch_id;
    active.stage = launch.stage || active.stage || "Preparing Run";
    active.status = launch.status || preparationState || "preparing";
    active.preparationAccepted = true;
    active.stopError = "";
    return "launch";
  }
  if (["preparing", "pending", "accepted"].includes(preparationState)) {
    active.stage = payload.stage || active.stage || "Preparing Run";
    active.message = payload.message
      ? publicStudyMessage(payload.message)
      : active.message || "OptPilot is checking this Study and preparing exact Run inputs. You can leave this page while it prepares.";
    active.preparationAccepted = true;
    return "preparing";
  }
  if (preparationState === "uncertain") {
    active.stage = payload.stage || "Confirming launch request";
    active.message = payload.message
      ? publicStudyMessage(payload.message)
      : "OptPilot is confirming whether this same launch request was accepted.";
    active.preparationAccepted = true;
    return "uncertain";
  }
  if (preparationState === "ready") {
    throw new Error("Study launch preparation is ready but has no launch record.");
  }
  return payload.error ? "legacy_error" : "unknown";
}

async function submitStudyLaunch(active, generation) {
  while (generation === state.studyLaunchPollGeneration && state.studyLaunch === active) {
    try {
      const payload = await postJson("/api/studies/launch", active.request, { tolerateError: true });
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      const outcome = applyStudyLaunchPreparationPayload(active, payload);
      if (outcome === "failed") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        return;
      }
      if (outcome === "launch") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        if (await handoffStudyLaunchIfReady(active, generation)) return;
        await pollStudyLaunch(active, generation);
        return;
      }
      if (outcome === "preparing") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        await pollStudyLaunchRequest(active, generation);
        return;
      }
      if (outcome === "uncertain") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        await sleep(1200);
        continue;
      }
      if (outcome === "legacy_error") {
        const durableRejection = payload.schema === "optpilot.studio-study-launch-response.v1";
        if (!durableRejection && payload.type !== "CoordinationConflict") {
          active.stage = "Reconciling launch preparation";
          if (state.view === "experiments") renderExperiments();
          await sleep(1200);
          continue;
        }
        active.failure = {
          code: payload.code || "study_launch_rejected",
          message: publicStudyMessage(payload.error),
        };
        active.stage = "Preparation failed";
        active.status = "failed";
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        return;
      }
      throw new Error("Study launch response is incomplete.");
    } catch (error) {
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      active.stage = "Reconnecting to launch preparation";
      active.stopError = "";
      if (state.view === "experiments") renderExperiments();
      await sleep(1500);
    }
  }
}

async function pollStudyLaunchRequest(active, generation, options = {}) {
  let immediate = options.immediate === true;
  while (generation === state.studyLaunchPollGeneration && state.studyLaunch === active) {
    if (!immediate) await sleep(800);
    immediate = false;
    try {
      const payload = await getJson(
        `/api/studies/launch-requests/${encodeURIComponent(active.requestId)}`
      );
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      const outcome = applyStudyLaunchPreparationPayload(active, payload);
      if (outcome === "failed") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        return;
      }
      if (outcome === "launch") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        if (await handoffStudyLaunchIfReady(active, generation)) return;
        await pollStudyLaunch(active, generation);
        return;
      }
      if (outcome === "uncertain") {
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        await sleep(1200);
        await submitStudyLaunch(active, generation);
        return;
      }
      if (outcome !== "preparing") {
        throw new Error("Study launch preparation status is incomplete.");
      }
      active.stopError = "";
      persistActiveStudyLaunch(active);
      if (state.view === "experiments") renderExperiments();
    } catch (error) {
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      active.stage = "Reconnecting to Run preparation";
      active.message = "OptPilot is reconnecting to this same launch request.";
      if (state.view === "experiments") renderExperiments();
      await sleep(1200);
      immediate = true;
    }
  }
}

async function pollStudyLaunch(active, generation, options = {}) {
  let immediate = options.immediate === true;
  while (generation === state.studyLaunchPollGeneration && state.studyLaunch === active) {
    if (!immediate) await sleep(800);
    immediate = false;
    try {
      const payload = await getJson(`/api/studies/launches/${encodeURIComponent(active.launchId)}`);
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      if (!payload || !payload.launch || payload.launch.launch_id !== active.launchId) {
        throw new Error("Study launch status is incomplete.");
      }
      active.launch = payload.launch;
      active.stage = payload.launch.stage || active.stage;
      active.stopError = "";
      persistActiveStudyLaunch(active);
      if (state.view === "experiments") renderExperiments();
      if (await handoffStudyLaunchIfReady(active, generation)) return;
      if (["failed", "cancelled"].includes(String(payload.launch.status || ""))) {
        active.failure = payload.launch.failure || (payload.launch.status === "cancelled"
          ? { code: "cancelled", message: "Run preparation was stopped." }
          : { code: "study_launch_failed", message: "OptPilot could not prepare this Run." });
        active.status = payload.launch.status;
        persistActiveStudyLaunch(active);
        if (state.view === "experiments") renderExperiments();
        return;
      }
    } catch (error) {
      if (generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return;
      active.stage = "Reconnecting to launch preparation";
      if (state.view === "experiments") renderExperiments();
      await sleep(1200);
    }
  }
}

async function handoffStudyLaunchIfReady(active, generation) {
  const runId = String(active.launch && active.launch.run_id || "");
  if (!runId || generation !== state.studyLaunchPollGeneration || state.studyLaunch !== active) return false;
  const shouldOpenRun = state.view === "experiments" && state.selectedPlanId === active.planId;
  state.studyLaunchPollGeneration += 1;
  state.studyLaunch = null;
  persistActiveStudyLaunch(null);
  if (shouldOpenRun) {
    state.selectedRunId = runId;
    state.selectedRun = null;
  }
  await loadRunsAndJobs();
  if (!shouldOpenRun) return true;
  setView("runs");
  await loadRunDetail(runId, { keepTab: true, skipListRender: true }).catch(() => {
    renderRunDetail();
  });
  return true;
}

async function stopActiveStudyLaunch() {
  const active = state.studyLaunch;
  const launch = active && active.launch;
  if (!active || !launch || !launch.launch_id || !launch.can_stop || active.stopPending) return;
  active.stopRequestId = active.stopRequestId || newRequestId();
  active.stopPending = true;
  active.stopError = "";
  persistActiveStudyLaunch(active);
  renderExperiments();
  try {
    const payload = await postJson(
      `/api/studies/launches/${encodeURIComponent(launch.launch_id)}/stop`,
      {
        schema: "optpilot.studio-study-launch-stop-request.v1",
        request_id: active.stopRequestId,
      },
    );
    if (!payload || !payload.launch || payload.launch.launch_id !== launch.launch_id) {
      throw new Error("Stop response does not match this launch.");
    }
    active.launch = payload.launch;
    active.stage = payload.launch.stage || "Stopping";
    active.stopPending = false;
    persistActiveStudyLaunch(active);
  } catch (error) {
    active.stopPending = false;
    active.stopError = publicStudyMessage(error && error.message || "Stop failed.");
  }
  if (state.view === "experiments") renderExperiments();
}

function isEmbeddedCodeWorkspaceActive() {
  return Boolean(state.embeddedCodeUrl || els.embeddedCodeWorkspace && els.embeddedCodeWorkspace.getAttribute("src"));
}

function codeFolderForSession(session) {
  return session && (session.codeFolder || session.path);
}

function codeWorkspaceOpenKey(session) {
  if (!session) return "";
  const workspaceId = session.backendWorkspaceId || session.id;
  return workspaceId ? `workspace:${workspaceId}` : codeFolderForSession(session);
}

function workspacePreviewKey(session = currentSession()) {
  return session ? session.backendWorkspaceId || session.id : "";
}

function workspaceInterfaceSelectionKey(session = currentSession()) {
  const workspaceId = session && (session.backendWorkspaceId || session.id);
  return workspaceId ? `workspace:${workspaceId}:interface-profile` : "";
}

function workspaceInterfaceConfig(session = currentSession()) {
  const iface = session && session.interface && typeof session.interface === "object" ? session.interface : null;
  const profiles = summarizedInterfaceProfiles(iface);
  return selectedInterfaceProfile(profiles, workspaceInterfaceSelectionKey(session), iface && iface.defaultProfileId || "");
}

function workspaceInterfaceLaunchCapability(session = currentSession(), profile = null) {
  const iface = session && session.interface && typeof session.interface === "object"
    ? session.interface
    : null;
  return interfaceProfileLaunchCapability(
    profile || workspaceInterfaceConfig(session),
    iface,
  );
}

function workspaceInterfaceLaunchKey(session = currentSession()) {
  const workspaceId = session && (session.backendWorkspaceId || session.id);
  const profile = workspaceInterfaceConfig(session);
  return workspaceId && profile ? `workspace:${workspaceId}:interface:${profile.id}` : "";
}

function workspaceSessionByBackendId(workspaceId) {
  return state.sessions.find((session) => String(session.backendWorkspaceId || session.id) === String(workspaceId || "")) || null;
}

function currentWorkspaceInterfaceLaunch(session = currentSession()) {
  const launch = state.interfaceLaunch;
  const workspaceId = session && (session.backendWorkspaceId || session.id);
  if (!launch || !workspaceId || launch.launch_scope !== "workspace-transient") return null;
  if (String(launch.source_workspace_id || "") === String(workspaceId)) return launch;
  return launch.key === workspaceInterfaceLaunchKey(session) ? launch : null;
}

function currentWorkspacePreview(session = currentSession()) {
  const key = workspacePreviewKey(session);
  if (!key) return { port: 5173, url: "", status: "idle", message: "" };
  if (!state.workspacePreviews[key]) {
    state.workspacePreviews[key] = { port: 5173, url: "", status: "idle", message: "" };
  }
  return state.workspacePreviews[key];
}

function previewPortValue() {
  const raw = Number(els.workspacePreviewPort && els.workspacePreviewPort.value || 5173);
  if (!Number.isFinite(raw)) return 5173;
  return Math.max(1, Math.min(65535, Math.trunc(raw)));
}

function updateWorkspacePreviewPort() {
  const session = currentSession();
  if (!session) return;
  const preview = currentWorkspacePreview(session);
  preview.port = previewPortValue();
  preview.message = preview.url ? `Port changed to ${preview.port}. Open Preview to update the frame.` : "";
  renderPreviewWorkbench();
}

function shouldAutoOpenCodeWorkspace(session = currentSession()) {
  if (!session || state.view !== "workspace" || state.workbenchMode !== "code") return false;
  if (state.codeWorkspacePaused || state.codeWorkspaceStatus === "opening" || state.codeWorkspaceStatus === "error") return false;
  return state.embeddedCodeFolder !== codeWorkspaceOpenKey(session) || !state.embeddedCodeUrl;
}

function maybeAutoOpenCodeWorkspace(session = currentSession()) {
  if (shouldAutoOpenCodeWorkspace(session)) {
    openCodeServerEmbedded();
  }
}

async function startCodeWorkspaceFromUser() {
  if (!currentSession()) {
    createBlankSession();
    return;
  }
  state.codeWorkspacePaused = false;
  state.codeWorkspaceStatus = "idle";
  state.codeWorkspaceMessage = "";
  await openCodeServerEmbedded();
}

function codeWorkspaceRequestIsCurrent(requestSeq, sessionId) {
  return requestSeq === state.codeWorkspaceRequestSeq
    && String(state.selectedSessionId || "") === String(sessionId || "");
}

function settleSupersededCodeWorkspaceRequest(requestSeq) {
  if (requestSeq !== state.codeWorkspaceRequestSeq) return;
  state.codeWorkspaceStatus = "idle";
  state.codeWorkspaceMessage = "";
  renderWorkspace();
}

async function openCatalogSourceCode(session, requestSeq) {
  const component = catalogSourceComponent(session);
  if (!component) {
    throw new Error("This published Catalog version is no longer available. Return to the Catalog item and try again.");
  }
  const componentKey = componentLaunchKey(component);
  const payload = await postJson(
    `/api/catalog/${encodeURIComponent(component.kind)}/${encodeURIComponent(component.entry.uid)}/open-code`,
    {},
  );
  const codeServer = payload && payload.code_server && typeof payload.code_server === "object"
    ? payload.code_server
    : null;
  if (!payload || !payload.workspace || !codeServer) {
    throw new Error("Studio could not reopen this published Catalog version in Code Server.");
  }
  if (String(codeServer.workspace_id || "") !== String(payload.workspace.id || "")) {
    throw new Error("Code Server returned a different Catalog source.");
  }
  if (!codeServer.open_url) {
    throw new Error(codeServer.error || codeServer.install_hint || "Code Server did not return an editor URL.");
  }
  if (!codeWorkspaceRequestIsCurrent(requestSeq, session.id)) return null;
  const refreshed = mergeUiWorkspace(payload.workspace);
  if (!refreshed || !isCatalogSourceView(refreshed)) {
    throw new Error("Studio did not return the expected read-only Catalog source.");
  }
  refreshed.catalogComponentKey = componentKey;
  state.selectedComponentKey = component.key;
  state.selectedSessionId = refreshed.id;
  state.selectedFileKey = refreshed.files[state.selectedFileKey]
    ? state.selectedFileKey
    : firstFileKey(refreshed);
  return { session: refreshed, codeServer };
}

async function openCodeServerEmbedded() {
  let session = currentSession();
  if (!session) return;
  const requestedSessionId = session.id;
  const requestSeq = ++state.codeWorkspaceRequestSeq;
  let folder = codeFolderForSession(session);
  let openKey = codeWorkspaceOpenKey(session);
  state.workbenchMode = "code";
  if (state.embeddedCodeUrl && state.embeddedCodeFolder === openKey) {
    state.codeWorkspaceStatus = "ready";
    state.codeWorkspacePaused = false;
    renderWorkspace();
    return;
  }
  state.embeddedCodeUrl = "";
  state.embeddedCodeFolder = "";
  if (els.embeddedCodeWorkspace) els.embeddedCodeWorkspace.removeAttribute("src");
  state.codeWorkspaceStatus = "opening";
  state.codeWorkspaceMessage = `Opening ${shortPath(folder)}.`;
  state.codeWorkspacePaused = false;
  session.timeline.push(["tool", "code-server", `Embedding ${shortPath(folder)}.`]);
  renderWorkspace();
  try {
    let result;
    if (isCatalogSourceView(session)) {
      const opened = await openCatalogSourceCode(session, requestSeq);
      if (!opened) {
        settleSupersededCodeWorkspaceRequest(requestSeq);
        return;
      }
      session = opened.session;
      result = opened.codeServer;
      folder = codeFolderForSession(session);
      openKey = codeWorkspaceOpenKey(session);
    } else {
      const workspaceKey = session.backendWorkspaceId || session.id;
      result = workspaceKey
        ? await postJson(`/api/workspaces/${encodeURIComponent(workspaceKey)}/open-code`, {}, { tolerateError: true })
        : await postJson("/api/code-server/start", { folder }, { tolerateError: true });
      if (!codeWorkspaceRequestIsCurrent(requestSeq, requestedSessionId)) {
        settleSupersededCodeWorkspaceRequest(requestSeq);
        return;
      }
    }
    state.codeServer = result;
    if (!result.open_url) {
      throw new Error(result.error || result.install_hint || "Install coder/code-server and refresh.");
    }
    state.embeddedCodeUrl = result.open_url;
    state.embeddedCodeFolder = openKey;
    state.codeWorkspaceStatus = "ready";
    state.codeWorkspaceMessage = "";
    els.embeddedCodeWorkspace.src = result.open_url;
    session.timeline.push(["tool", "code-server embedded", `Folder: ${shortPath(result.folder || folder)}.`]);
  } catch (error) {
    if (!codeWorkspaceRequestIsCurrent(requestSeq, requestedSessionId)) {
      settleSupersededCodeWorkspaceRequest(requestSeq);
      return;
    }
    state.codeWorkspaceStatus = "error";
    state.codeWorkspaceMessage = boundedPublicActionError(
      error,
      isCatalogSourceView(session)
        ? "This published Catalog version could not be opened. Try again."
        : "Code Server could not open this Workspace. Try again.",
    );
    session.timeline.push(["tool", "code-server unavailable", state.codeWorkspaceMessage]);
  }
  renderWorkspace();
}

function setWorkspaceActionNotice(title, body, error = false, session = currentSession()) {
  if (!session) return;
  state.workspaceNotice = {
    workspaceId: session.id,
    title,
    body,
    error,
  };
  renderWorkspace();
}

function reserveExternalWindow() {
  try {
    const externalWindow = window.open("about:blank", "_blank");
    if (externalWindow) externalWindow.opener = null;
    return externalWindow;
  } catch (_error) {
    return null;
  }
}

function navigateExternalWindow(externalWindow, url) {
  if (!externalWindow || !url) return false;
  try {
    externalWindow.location.replace(url);
    return true;
  } catch (_error) {
    try {
      externalWindow.location.href = url;
      return true;
    } catch (_nestedError) {
      return false;
    }
  }
}

function closeReservedExternalWindow(externalWindow) {
  if (!externalWindow) return;
  try {
    externalWindow.close();
  } catch (_error) {
    // A browser may revoke access after navigation. There is nothing else to clean up.
  }
}

async function openCodeServerFull() {
  let session = currentSession();
  if (!session) return;
  const requestedSessionId = session.id;
  const catalogSourceView = isCatalogSourceView(session);
  const externalWindow = reserveExternalWindow();
  if (!externalWindow) {
    setWorkspaceActionNotice(
      "New window blocked",
      "Allow popups for OptPilot, then choose Open editor in new window again.",
      true,
      session,
    );
    return;
  }
  let openKey = codeWorkspaceOpenKey(session);
  if (state.embeddedCodeUrl && state.embeddedCodeFolder === openKey) {
    if (!navigateExternalWindow(externalWindow, state.embeddedCodeUrl)) {
      closeReservedExternalWindow(externalWindow);
      setWorkspaceActionNotice(
        "Editor could not be opened",
        "The browser did not allow OptPilot to open the editor URL.",
        true,
        session,
      );
    }
    return;
  }
  const requestSeq = ++state.codeWorkspaceRequestSeq;
  try {
    let result;
    if (isCatalogSourceView(session)) {
      const opened = await openCatalogSourceCode(session, requestSeq);
      if (!opened) {
        closeReservedExternalWindow(externalWindow);
        settleSupersededCodeWorkspaceRequest(requestSeq);
        return;
      }
      session = opened.session;
      result = opened.codeServer;
      openKey = codeWorkspaceOpenKey(session);
    } else {
      const workspaceKey = session.backendWorkspaceId || session.id;
      result = workspaceKey
        ? await postJson(`/api/workspaces/${encodeURIComponent(workspaceKey)}/open-code`, {}, { tolerateError: true })
        : await postJson("/api/code-server/start", { folder: session.codeFolder || session.path }, { tolerateError: true });
      if (!codeWorkspaceRequestIsCurrent(requestSeq, requestedSessionId)) {
        closeReservedExternalWindow(externalWindow);
        settleSupersededCodeWorkspaceRequest(requestSeq);
        return;
      }
    }
    state.codeServer = result;
    if (!result.open_url || !navigateExternalWindow(externalWindow, result.open_url)) {
      closeReservedExternalWindow(externalWindow);
      setWorkspaceActionNotice(
        "Editor could not be opened",
        boundedPublicActionError(
          result.error || result.install_hint || "Code Server did not return an editor URL.",
          "Code Server did not return an editor URL.",
        ),
        true,
        session,
      );
      return;
    }
    state.embeddedCodeUrl = result.open_url;
    state.embeddedCodeFolder = openKey;
    state.codeWorkspaceStatus = "ready";
    state.codeWorkspaceMessage = "";
    renderWorkspace();
  } catch (error) {
    closeReservedExternalWindow(externalWindow);
    if (!codeWorkspaceRequestIsCurrent(requestSeq, requestedSessionId)) {
      settleSupersededCodeWorkspaceRequest(requestSeq);
      return;
    }
    setWorkspaceActionNotice(
      "Editor could not be opened",
      boundedPublicActionError(
        error,
        catalogSourceView
          ? "This published Catalog version could not be opened. Try again."
          : "Code Server could not open this Workspace. Try again.",
      ),
      true,
      session,
    );
  }
}

function reloadEmbeddedCodeWorkspace() {
  if (state.embeddedCodeUrl) {
    els.embeddedCodeWorkspace.src = state.embeddedCodeUrl;
  }
}

async function launchActiveWorkbenchInterface() {
  if (isCatalogSourceView()) {
    const component = catalogSourceComponent();
    if (component) await launchComponentInterface(component);
    return;
  }
  await launchWorkspaceInterface();
}

async function launchWorkspaceInterface() {
  const session = currentSession();
  const workspaceInterface = workspaceInterfaceConfig(session);
  if (!session || !workspaceInterface) return;
  const capability = workspaceInterfaceLaunchCapability(
    session,
    workspaceInterface,
  );
  if (capability.eligible !== true) return;
  const workspaceId = session.backendWorkspaceId || session.id;
  const launchKey = workspaceInterfaceLaunchKey(session);
  const previousLaunch = state.interfaceLaunch;
  const retryingFailedLaunch = Boolean(
    previousLaunch
    && previousLaunch.status === "failed",
  );
  if (previousLaunch && !retryingFailedLaunch) {
    const preview = currentWorkspacePreview(session);
    preview.status = "blocked";
    preview.message = previousLaunch.key === launchKey
      ? `${previousLaunch.label || "This interface"} is already running.`
      : `${previousLaunch.label || "Another interface"} is already running in this tab. Stop it before launching this Workspace’s interface.`;
    state.workbenchMode = "preview";
    renderWorkspace();
    return;
  }
  if (retryingFailedLaunch) {
    state.interfaceLaunch = null;
    persistActiveInterfaceLaunch(null);
  }
  resetActiveInterfaceReturnState();
  const preview = currentWorkspacePreview(session);
  preview.port = Number(workspaceInterface.presentation.port || preview.port || 5173);
  state.interfaceLaunch = {
    key: launchKey,
    label: workspaceInterface.label || "interface",
    port: workspaceInterface.presentation.port,
    profile_id: workspaceInterface.id,
    launch_scope: "workspace-transient",
    source_workspace_id: workspaceId,
    startedAt: Date.now(),
    status: "queued",
    error: "",
  };
  preview.status = "opening";
  preview.message = `Launching ${workspaceInterface.label || "interface"} from this workspace.`;
  preview.url = "";
  state.workbenchMode = "preview";
  session.timeline.push(["tool", "interface launch", `Starting ${workspaceInterface.label || "interface"} (${workspaceInterface.id}) on port ${workspaceInterface.presentation.port}.`]);
  renderWorkspace();
  try {
    const payload = await postJson(`/api/workspaces/${encodeURIComponent(workspaceId)}/launch-interface-job`, {
      setup: "auto",
      profile_id: workspaceInterface.id,
    });
    const launch = payload.launch || {};
    state.interfaceLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey);
    if (state.interfaceLaunch.launch_scope !== "workspace-transient"
      || String(state.interfaceLaunch.source_workspace_id || "") !== String(workspaceId)) {
      throw new Error("Interface launch did not preserve the source workspace boundary.");
    }
    renderWorkspace();
    await pollWorkspaceInterfaceLaunch(launchKey, state.interfaceLaunch.launch_id);
  } catch (error) {
    if (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
      state.interfaceLaunch = {
        ...state.interfaceLaunch,
        status: "failed",
        error: boundedPublicActionError(error, "This interface could not be started."),
      };
      persistActiveInterfaceLaunch(null);
    }
    preview.status = "error";
    preview.message = boundedPublicActionError(error, "This interface could not be started.");
    session.timeline.push(["tool", "interface launch failed", preview.message]);
    renderWorkspace();
  }
}

async function pollWorkspaceInterfaceLaunch(launchKey, launchId) {
  if (!launchId) throw new Error("Interface launch did not return a launch id.");
  let readyObserved = false;
  while (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
    let payload;
    try {
      payload = await getJson(`/api/interface-launches/${encodeURIComponent(launchId)}`);
    } catch (error) {
      if (!readyObserved) throw error;
      await sleep(1000);
      continue;
    }
    const launch = payload.launch || {};
    if (!state.interfaceLaunch || state.interfaceLaunch.key !== launchKey || state.interfaceLaunch.launch_id !== launchId) return;
    const expectedWorkspaceId = state.interfaceLaunch.source_workspace_id;
    state.interfaceLaunch = mergeInterfaceLaunchPayload(state.interfaceLaunch, launch, launchKey);
    if (state.interfaceLaunch.launch_scope !== "workspace-transient"
      || String(state.interfaceLaunch.source_workspace_id || "") !== String(expectedWorkspaceId || "")) {
      throw new Error("Interface launch escaped its source workspace boundary.");
    }
    const sourceWorkspaceId = state.interfaceLaunch.source_workspace_id;
    let sourceSession = workspaceSessionByBackendId(sourceWorkspaceId);
    if (!sourceSession) throw new Error("The source workspace is no longer available.");
    const selectedSession = currentSession();
    const sourceIsSelected = Boolean(selectedSession
      && String(selectedSession.backendWorkspaceId || selectedSession.id) === String(sourceWorkspaceId));
    const status = state.interfaceLaunch.status;
    const step = (state.interfaceLaunch.steps || []).slice(-1)[0];
    if (status === "ready") {
      const firstReady = !readyObserved;
      const result = state.interfaceLaunch.result || {};
      if (!result.preview || !result.preview.preview_url) {
        throw new Error("Interface launch completed without a Preview URL.");
      }
      if (firstReady && result.workspace) {
        if (String(result.workspace.id || "") !== String(sourceWorkspaceId)) {
          throw new Error("Interface launch returned a different workspace.");
        }
        sourceSession = mergeUiWorkspace(result.workspace) || sourceSession;
      }
      if (firstReady) {
        applyWorkspacePreviewPayload(sourceSession, result.preview, result.interface);
        if (sourceIsSelected) renderWorkspace();
      } else if (sourceIsSelected) {
        updateInterfaceOutputPanel(state.interfaceLaunch, els.workspaceInterfaceLaunchStatus);
      }
      readyObserved = true;
      await sleep(1000);
      continue;
    }
    if (status === "cleanup_pending") {
      clearWorkspaceInterfacePreview(state.interfaceLaunch, "error", "Interface stopped, but launch-scoped cleanup is still pending.");
      if (sourceIsSelected) renderWorkspace();
      return;
    }
    if (status === "stopped") {
      clearWorkspaceInterfacePreview(state.interfaceLaunch, "idle", "Interface stopped. Launch it again when you want to inspect this workspace.");
      closeInterfaceStopConfirmation();
      state.interfaceLaunch = null;
      resetActiveInterfaceReturnState();
      persistActiveInterfaceLaunch(null);
      if (sourceIsSelected) renderWorkspace();
      return;
    }
    if (status === "failed") {
      closeInterfaceStopConfirmation();
      throw new Error(state.interfaceLaunch.error || "Interface launch failed.");
    }
    const preview = currentWorkspacePreview(sourceSession);
    preview.status = "opening";
    preview.message = step && (step.detail || step.title) || "Launching workspace interface.";
    if (sourceIsSelected) renderWorkspace();
    await sleep(1000);
  }
}

async function openWorkspacePreview() {
  const session = currentSession();
  if (!session) return;
  const preview = currentWorkspacePreview(session);
  const folder = codeFolderForSession(session);
  const port = previewPortValue();
  preview.port = port;
  preview.status = "opening";
  preview.message = `Opening port ${port} through the workspace runtime.`;
  preview.url = "";
  state.workbenchMode = "preview";
  session.timeline.push(["tool", "workspace preview", `Opening ${shortPath(folder)} on port ${port}.`]);
  renderWorkspace();
  const result = await postJson("/api/workspace-preview/open", { folder, port }, { tolerateError: true });
  if (result.preview_url) {
    preview.url = result.preview_url;
    preview.status = "ready";
    preview.message = `Previewing port ${port} through ${shortPath(result.folder || folder)}.`;
    if (result.code_server) {
      state.codeServer = result.code_server;
      if (result.code_server.open_url) {
        state.embeddedCodeUrl = result.code_server.open_url;
        state.embeddedCodeFolder = folder;
        state.codeWorkspaceStatus = "ready";
        state.codeWorkspaceMessage = "";
      }
    }
    session.timeline.push(["tool", "workspace preview ready", `URL: ${result.preview_url}`]);
  } else {
    preview.status = "error";
    preview.message = result.error || "Preview could not be opened.";
    session.timeline.push(["tool", "workspace preview unavailable", preview.message]);
  }
  renderWorkspace();
}

function reloadWorkspacePreview() {
  const preview = currentWorkspacePreview();
  if (!preview.url || !els.workspacePreviewFrame) return;
  els.workspacePreviewFrame.removeAttribute("src");
  window.requestAnimationFrame(() => {
    els.workspacePreviewFrame.src = preview.url;
  });
}

function openWorkspacePreviewExternal() {
  const session = currentSession();
  const catalogSourceView = isCatalogSourceView(session);
  const preview = currentWorkspacePreview(session);
  const previewUrl = catalogSourceView ? catalogInterfacePreviewUrl(session) : preview.url;
  if (!session || !previewUrl) {
    setWorkspaceActionNotice(
      "Interface is not open",
      catalogSourceView
        ? "Start the published interface first, then open it in a new window."
        : "Launch the Workspace interface first, then open it in a new window.",
      true,
      session,
    );
    return;
  }
  const externalWindow = reserveExternalWindow();
  if (!externalWindow) {
    setWorkspaceActionNotice(
      "New window blocked",
      "Allow popups for OptPilot, then choose Open interface in new window again.",
      true,
      session,
    );
    return;
  }
  if (!navigateExternalWindow(externalWindow, previewUrl)) {
    closeReservedExternalWindow(externalWindow);
    setWorkspaceActionNotice(
      "Interface could not be opened",
      "The browser did not allow OptPilot to open the interface URL.",
      true,
      session,
    );
  }
}

async function openActiveWorkspaceExternal() {
  if (state.workbenchMode === "preview") {
    openWorkspacePreviewExternal();
    return;
  }
  if (state.workbenchMode === "setup") {
    setWorkspaceActionNotice(
      "Nothing to open in a new window",
      "Publish stays inside Studio. Choose Code or Interface to open that view separately.",
      false,
    );
    return;
  }
  await openCodeServerFull();
}

async function stopCodeServer() {
  const result = await postJson("/api/code-server/stop", {}, { tolerateError: true });
  state.codeServer = result;
  state.embeddedCodeUrl = "";
  state.embeddedCodeFolder = "";
  state.codeWorkspaceStatus = "paused";
  state.codeWorkspaceMessage = "";
  state.codeWorkspacePaused = true;
  els.embeddedCodeWorkspace.removeAttribute("src");
  renderWorkspace();
}

async function handleAgentActionButton() {
  if (assistantIsBusy()) {
    await cancelAgentMessage();
    return;
  }
  if (assistantIsAwaitingApproval()) return;
  await sendAgentMessage();
}

async function cancelAgentMessage() {
  const session = currentAgentSession();
  if (!session || !session.id || session.id.startsWith("agent-session-")) return;
  if (state.cancellingAgentSessionIds.has(session.id)) return;
  state.cancellingAgentSessionIds.add(session.id);
  updateAssistantComposerState();
  try {
    const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(session.id)}/cancel`, {});
    if (payload.session) await updateAgentSessionFromPayload(payload.session);
    await loadAgentSessions();
  } catch (error) {
    pushAssistantMessage(["tool", "Stop failed", String(error.message || error)]);
  } finally {
    state.cancellingAgentSessionIds.delete(session.id);
    renderAssistant();
  }
}

async function sendAgentMessage() {
  if (assistantIsBusy() || assistantIsAwaitingApproval()) return;
  const message = els.agentInput.value.trim();
  if (!message) return;
  const userMessage = ["user", "User", message];
  pushAssistantMessage(userMessage);
  const session = currentAgentSession();
  if (session && !session.id.startsWith("agent-session-")) session.status = "running";
  els.agentInput.value = "";
  delete els.agentInput.dataset.touched;
  renderAssistant();
  let persisted = null;
  try {
    persisted = await persistAssistantMessage(userMessage, {
      keepalive: true,
      sessionId: session && session.id,
      rethrowError: true,
    });
  } catch (error) {
    const reason = boundedPublicActionError(
      error,
      "The message was not sent. Refresh Studio and try again.",
    );
    if (/Assistant run selection|selected_run/i.test(reason)) {
      state.assistantRunSelection = null;
    }
    pushAssistantMessage([
      "assistant",
      "Message not sent",
      reason,
    ], { persist: false });
    renderAssistant();
    return;
  }
  if (!persisted) {
    pushAssistantMessage(["assistant", "Runtime unavailable", "This message is visible locally, but the Assistant conversation could not store it."]);
  }
  renderAssistant();
}

function planPayload(plan, options = {}) {
  const saveAsDraft = options.saveAsDraft === true;
  const payload = {
    environment_ref: exactCatalogEntryRef(plan.environment),
    method_ref: exactCatalogEntryRef(plan.method),
    name: plan.name || planName(plan),
    description: plan.description || "",
    tags: plan.tags || [],
    metric: plan.metric || firstMetric(plan.environment) || "score",
    direction: plan.direction || "maximize",
    aggregation: plan.aggregation || "mean",
    secondaryMetrics: plan.secondaryMetrics || [],
    maxTrials: Number(plan.maxTrials || 8),
    maxWallClockSeconds: positiveOptionalNumber(plan.maxWallClockSeconds),
    maxFailures: positiveOptionalNumber(plan.maxFailures),
    parallelism: Number(plan.parallelism || 1),
    timeoutSeconds: Number(plan.timeoutSeconds || 120),
    maxRetries: nonNegativeOptionalNumber(plan.maxRetries),
    evidenceLevel: plan.evidenceLevel || "standard",
    seed: plan.seed === "" || plan.seed === null || plan.seed === undefined ? null : Number(plan.seed),
    save_as_draft: saveAsDraft,
  };
  const savedDraft = Boolean(plan.draft && plan.draft.saved_as_draft);
  const reuseWorkspace = Boolean(
    plan.draft
    && plan.draft.workspace_id
    && (saveAsDraft ? savedDraft : !savedDraft),
  );
  if (reuseWorkspace) {
    payload.workspace_id = plan.draft.workspace_id;
    if (plan.draft.workspace_revision !== null && plan.draft.workspace_revision !== undefined) {
      payload.expected_workspace_revision = plan.draft.workspace_revision;
    }
  } else {
    const requestKey = saveAsDraft ? "draftSaveRequestId" : "launchPreparationRequestId";
    if (!plan[requestKey]) plan[requestKey] = newRequestId();
    payload.request_id = plan[requestKey];
  }
  if (saveAsDraft) {
    if (!plan.draftActionId) plan.draftActionId = newRequestId();
    payload.draft_action_id = plan.draftActionId;
    if (savedDraft && plan.draft.draft_revision) {
      payload.expected_draft_revision = plan.draft.draft_revision;
    }
  }
  return payload;
}

function exactCatalogEntryRef(entry) {
  if (!entry) return null;
  if (entry.ref) return entry.ref;
  return String(entry.uid || "").startsWith("cref_") ? entry.uid : null;
}

function upsertSession(session) {
  state.sessions = [session, ...state.sessions.filter((item) => item.id !== session.id)];
}

function upsertPlan(plan) {
  state.plans = [plan, ...state.plans.filter((item) => item.id !== plan.id)];
}

function currentSession() {
  return state.sessions.find((session) => session.id === state.selectedSessionId) || null;
}

function currentPlan() {
  return state.plans.find((plan) => plan.id === state.selectedPlanId) || state.plans[0] || null;
}

function allComponents() {
  return [
    ...(state.catalog.environments || []).map((entry) => ({ key: `environment:${entry.uid}`, kind: "environment", entry })),
    ...(state.catalog.methods || []).map((entry) => ({ key: `method:${entry.uid}`, kind: "method", entry })),
    ...(state.catalog.resources || []).map((entry) => ({ key: `resource:${entry.uid}`, kind: "resource", entry })),
  ];
}

function componentByKey(key) {
  return allComponents().find((component) => component.key === key) || null;
}

function catalogEntryByUid(kind, uid) {
  const entries = kind === "method" ? state.catalog.methods : state.catalog.environments;
  return (entries || []).find((entry) => entry.uid === uid) || null;
}

function catalogEntryByRef(kind, reference) {
  if (!reference) return null;
  const entries = kind === "method" ? state.catalog.methods : state.catalog.environments;
  if (typeof reference === "string") {
    return (entries || []).find((entry) => entry.uid === reference) || null;
  }
  const digest = reference && reference.ref_digest;
  if (!digest) return null;
  return (entries || []).find((entry) => entry.ref && entry.ref.ref_digest === digest) || null;
}

function catalogReference(kind, reference, authoredValue = "") {
  if (!reference && !authoredValue) return null;
  const authoredLabel = String(authoredValue || "").split("/").pop() || "";
  const id = reference && typeof reference === "object" && reference.entry_id
    ? String(reference.entry_id)
    : authoredLabel.replace(/\.ya?ml$/, "") || kind;
  return {
    id,
    uid: typeof reference === "string"
      ? reference
      : reference && reference.ref_digest
      ? `catalog-ref:${reference.ref_digest}`
      : "",
    label: authoredLabel || id,
    ref: reference || null,
    summary: {},
  };
}

function firstCompatiblePair() {
  return (state.compatibility.pairs || []).find((pair) => pair.compatible) || null;
}

function firstFileKey(session) {
  return session && Object.keys(session.files)[0];
}

function firstMetric(environment) {
  return (environment.summary && environment.summary.metrics || [])[0] || "";
}

function preferredMetric(environment) {
  const metrics = environment.summary && environment.summary.metrics || [];
  return metrics.find((metric) => metric === "normalized_makespan")
    || metrics.find((metric) => /score|reward|accuracy|throughput|service/i.test(metric))
    || metrics[0]
    || "score";
}

function directionForMetric(metric) {
  return /makespan|tardiness|loss|cost|error|latency|time/i.test(metric) ? "minimize" : "maximize";
}

function planFromPair(pair) {
  const metric = preferredMetric(pair.environment);
  const metrics = pair.environment.summary && pair.environment.summary.metrics || [];
  const secondaryMetrics = metrics.filter((item) => item !== metric).slice(0, 4);
  const timeoutSeconds = pair.environment.summary && pair.environment.summary.runtime && pair.environment.summary.runtime.timeoutSeconds || 120;
  const plan = {
    environment: pair.environment,
    method: pair.method,
    name: `${pair.environment.id}-${pair.method.id}`,
    description: "",
    tags: [],
    metric,
    direction: directionForMetric(metric),
    aggregation: "mean",
    secondaryMetrics,
    maxTrials: 8,
    maxWallClockSeconds: "",
    maxFailures: "",
    parallelism: 1,
    timeoutSeconds,
    methodRequestTimeoutSeconds: 10,
    maxRetries: "",
    evidenceLevel: "standard",
    seed: 0,
  };
  return {
    ...plan,
    id: `pair-${slug(pair.environment.id)}-${slug(pair.method.id)}`,
    title: `${pair.environment.label} + ${pair.method.label}`,
    source: "draft config",
    status: "draft",
    checks: compatibilityChecks(pair),
    yaml: planYamlPreview(plan),
    draft: null,
  };
}

function planYamlPreview(plan) {
  const lines = [
    "apiVersion: optpilot.io/v1",
    "config: study",
    `name: ${yamlScalar(plan.name || planName(plan))}`,
  ];
  if (plan.description) lines.push(`description: ${yamlScalar(plan.description)}`);
  if ((plan.tags || []).length) {
    lines.push(`tags: [${plan.tags.map(yamlScalar).join(", ")}]`);
  }
  lines.push("");
  if (plan.environment) lines.push(`environmentConfig: ${yamlScalar(exactCatalogBindingPreview(plan.environment, "environment"))}`);
  if (plan.method) lines.push(`methodConfig: ${yamlScalar(exactCatalogBindingPreview(plan.method, "method"))}`);
  lines.push(
    "",
    "objective:",
    `  metric: ${plan.metric || "score"}`,
    `  direction: ${plan.direction || "maximize"}`,
    `  aggregation: ${plan.aggregation || "mean"}`,
  );
  if ((plan.secondaryMetrics || []).length) {
    lines.push(`  secondaryMetrics: [${plan.secondaryMetrics.join(", ")}]`);
  }
  lines.push(
    "",
    "budget:",
    `  maxTrials: ${Number(plan.maxTrials || 1)}`,
  );
  const maxWallClockSeconds = positiveOptionalNumber(plan.maxWallClockSeconds);
  if (maxWallClockSeconds !== null) {
    lines.push(`  maxWallClockSeconds: ${maxWallClockSeconds}`);
  }
  const maxFailures = positiveOptionalNumber(plan.maxFailures);
  if (maxFailures !== null) {
    lines.push(`  maxFailures: ${maxFailures}`);
  }
  lines.push(
    "",
    "execution:",
    `  parallelism: ${Number(plan.parallelism || 1)}`,
  );
  if (plan.timeoutSeconds !== "" && plan.timeoutSeconds !== null && plan.timeoutSeconds !== undefined) {
    lines.push(`  timeoutSeconds: ${Number(plan.timeoutSeconds || 0)}`);
  }
  const maxRetries = nonNegativeOptionalNumber(plan.maxRetries);
  if (maxRetries !== null) {
    lines.push("  retry:", `    maxRetries: ${maxRetries}`);
  }
  lines.push(
    "",
    "evidence:",
    `  level: ${plan.evidenceLevel || "standard"}`,
  );
  if (plan.seed !== "" && plan.seed !== null && plan.seed !== undefined) {
    lines.push("", "reproducibility:", `  seed: ${Number(plan.seed || 0)}`);
  }
  return lines.join("\n");
}

function exactCatalogBindingPreview(entry, kind) {
  const reference = exactCatalogEntryRef(entry);
  const sourceId = reference && typeof reference === "object" && reference.source_id || "catalog";
  const revision = reference && typeof reference === "object" && reference.source_revision || "current";
  const entryId = reference && typeof reference === "object" && reference.entry_id || entry && entry.id || kind;
  return `[exact ${kind}: ${sourceId}@${revision}/${entryId}]`;
}

function planName(plan) {
  if (plan && plan.environment && plan.method) return `${plan.environment.id}-${plan.method.id}`;
  return slug(plan && plan.title || "study");
}

function compatibilityChecks(pair) {
  const checks = pair.checks && pair.checks.length ? pair.checks : (pair.reasons || []).map((message) => ({ ok: pair.compatible, message }));
  return checks.map((check) => ["Compatibility", check.message, check.ok ? "compatible" : "review"]);
}

function studyReadinessPanel(plan) {
  const rows = studyReadinessRows(plan);
  if (!rows.length) return "";
  const launch = studyLaunchCapability(plan);
  const bindingReason = studyLaunchBindingReason(plan);
  const publicationReason = studyCatalogPublicationSetup(plan).reason;
  const runtimeSetupReason = studyRuntimeSetupReason(plan);
  const blockedReason = bindingReason
    || publicationReason
    || runtimeSetupReason
    || launch && launch.eligible !== true && publicStudyLaunchReason(launch)
    || "";
  return `
    <div class="readiness-panel">
      <div class="study-launch-status">
        <div>
          <h3>Launch status</h3>
          <p>${escapeHtml(blockedReason || "Ready to launch. OptPilot will check the current Study inputs before starting the Run.")}</p>
        </div>
        ${labeledStatusPill(
          blockedReason ? publicationReason || runtimeSetupReason ? "Setup needed" : "Needs review" : "Ready to launch",
          blockedReason ? publicationReason || runtimeSetupReason ? "setup" : "review" : "ready",
        )}
      </div>
      ${studyRuntimeEnvironmentRequirementsPanel(plan)}
      <div class="readiness-list">${rows.map(readinessRow).join("")}</div>
    </div>
  `;
}

function studyRuntimeEnvironmentRequirementsPanel(plan) {
  const requirements = studyRuntimeEnvironmentRequirements(plan);
  if (!requirements.length) return "";
  const missing = requirements.some((item) => !studyRuntimeRequirementConfigured(item));
  return `
    <section class="env-requirements-panel study-runtime-requirements">
      <div>
        <strong>Required local values</strong>
        <p>When you launch, Studio binds the current saved revision to the new Run. The value is handed only to this Run's Method process and is not copied into the Run record. Later Settings changes apply only to new Runs.</p>
      </div>
      <div class="env-requirements-list">
        ${requirements.map((item) => {
          const isConfigured = studyRuntimeRequirementConfigured(item);
          return `
            <div class="env-requirement-row">
              <span>
                <strong>${escapeHtml(item.name)}</strong>
                <small>Method process · current Settings revision bound at launch</small>
              </span>
              ${statusPill(isConfigured ? "configured" : "missing")}
            </div>
          `;
        }).join("")}
      </div>
      ${missing ? `<button class="ghost-button study-open-environment-settings" type="button">Open Studio Settings</button>` : ""}
    </section>
  `;
}

function studyValidationPanel(plan) {
  const validation = plan && plan.draft && plan.draft.validation || plan && plan.validation || plan && plan.study && plan.study.validation;
  if (!validation) return "";
  return `
    <section class="study-card study-validation-section">
      <h3>Validation</h3>
      <div class="validation-box">${validationHtml(publicStudyValidation(validation))}</div>
    </section>
  `;
}

function studyBindingReason(plan) {
  if (!plan || !plan.environment && !plan.method) return "Choose an Environment and a Method.";
  if (!plan.environment) return "Choose an Environment.";
  if (!plan.method) return "Choose a Method.";
  return "";
}

function studyLaunchBindingReason(plan) {
  if (plan && plan.study && (plan.study.ref || plan.study.uid)) return "";
  return studyBindingReason(plan);
}

function publicStudyValidation(validation) {
  if (!validation || typeof validation !== "object") return validation;
  const launch = validation.launch && typeof validation.launch === "object"
    ? { ...validation.launch, reason: publicStudyLaunchReason(validation.launch) }
    : validation.launch;
  return {
    ...validation,
    errors: (validation.errors || []).map(publicStudyMessage),
    launch,
  };
}

function publicStudyLaunchReason(capability) {
  const code = capability && capability.code || "";
  if (code === "runtime_environment_missing") {
    return "Add the required local value in Studio Settings before launching this Run.";
  }
  if (code === "method_mode_unsupported") {
    return "This Method uses an execution mode that Study Runs do not currently support.";
  }
  if (code === "method_callable_unchecked") {
    return "This Method needs a supported runnable test before it can be used in a Study Run.";
  }
  if (code === "retained_preflight_missing" || code === "retained_preflight_failed") {
    return "OptPilot could not confirm that this Study can run. Check the Environment and Method, then try again.";
  }
  return publicStudyMessage(
    capability && (capability.reason || capability.code)
      || "This Environment and Method cannot currently launch a Study Run.",
  );
}

function publicStudyMessage(value) {
  return String(value || "")
    .replace(/retained process-study runner/gi, "current Study runner")
    .replace(/retained Python batch worker/gi, "current Python Study runner")
    .replace(/retained batch method/gi, "Study Method")
    .replace(/retained method/gi, "Method")
    .replace(/retained execution/gi, "Study execution")
    .replace(/retained package/gi, "fixed Study inputs")
    .replace(/retained runner/gi, "Study runner")
    .replace(/retained study/gi, "Study")
    .replace(/canonical Realm Workbench/gi, "Run page")
    .replace(/canonical Realm/gi, "Run")
    .replace(/\bRealm\b/g, "OptPilot");
}

function studyLaunchCapability(plan) {
  const validation = plan && plan.draft && plan.draft.validation || plan && plan.validation || plan && plan.study && plan.study.validation;
  if (!validation) return null;
  if (validation.launch && typeof validation.launch === "object") {
    if (
      validation.launch.code === "runtime_environment_missing"
      && !studyRuntimeSetupReason(plan)
    ) {
      return { eligible: true, code: "ready", reason: null };
    }
    return validation.launch;
  }
  const retained = validation.capabilities && validation.capabilities.retained_execution;
  if (!retained || typeof retained !== "object") return null;
  return {
    eligible: retained.eligible === true,
    code: retained.code || "retained_execution_unsupported",
    reason: retained.reason || "Retained execution is unavailable for this study.",
  };
}

function studyReadinessRows(plan) {
  const rows = [];
  const publicationReason = studyCatalogPublicationSetup(plan).reason;
  if (plan.environment && plan.method) {
    rows.push(["Environment and Method", `${plan.environment.label || plan.environment.id} + ${plan.method.label || plan.method.id}`, "ready"]);
    rows.push(["Environment runtime", componentExecutionSummary(plan.environment.raw_config || {}), "ready"]);
    rows.push(["Method runtime", componentExecutionSummary(plan.method.raw_config || {}), "ready"]);
  } else if (plan.study && (plan.study.ref || plan.study.uid)) {
    rows.push(["Environment and Method", "Defined by this built-in Study.", "ready"]);
  } else {
    rows.push(["Environment and Method", studyBindingReason(plan), "review"]);
  }
  for (const check of plan.checks || []) rows.push(check);
  if (publicationReason) {
    rows.push(["Catalog version", publicationReason, "review"]);
  }
  rows.push(["Study inputs", "Launching fixes the selected component versions and settings for this Run.", "ready"]);
  rows.push(["Run results", "The Run records Candidates, trials, metrics, and declared outputs.", "ready"]);
  if (plan.study && (plan.study.ref || plan.study.uid)) {
    rows.push(["Study", "Loaded from a built-in Study. Save a draft if you want to reopen your changes later.", "ready"]);
  } else if (plan.draft && plan.draft.validation) {
    const valid = Boolean(plan.draft.validation.valid);
    rows.push(["Study", valid ? "The saved settings passed validation." : "The saved settings need review.", valid ? "valid" : "review"]);
  } else {
    rows.push(["Study", "Launch will check these settings before starting the Run.", "ready"]);
  }
  const launch = studyLaunchCapability(plan);
  if (launch) {
    rows.push([
      "Run support",
      launch.eligible === true
        ? "The selected Environment and Method can launch with the current Study runner. This check did not execute their code."
        : publicStudyLaunchReason(launch),
      launch.eligible === true ? "ready" : "review",
    ]);
  }
  return rows;
}

function readinessRow([label, value, status]) {
  return `
    <div class="readiness-row">
      <div>
        <strong>${escapeHtml(label)}</strong>
        <span>${escapeHtml(value)}</span>
      </div>
      ${statusPill(status)}
    </div>
  `;
}

function compatibleMethodsForEnvironment(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.environment.uid === uid && pair.compatible);
}

function compatibleEnvironmentsForMethod(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.method.uid === uid && pair.compatible);
}

function entityHeader(item, kind) {
  return `
    <div class="detail-heading">
      <div class="detail-title-block">
        <div class="detail-title-line">
          <h2>${escapeHtml(item.label)}</h2>
          <span class="catalog-kind-chip catalog-kind-${escapeHtml(kind)}">${escapeHtml(catalogKindLabel(kind, item))}</span>
        </div>
        <p class="path-text">${escapeHtml(shortPath(item.path))}</p>
      </div>
    </div>
    ${item.description ? `<p class="detail-description">${escapeHtml(item.description)}</p>` : ""}
  `;
}

function sessionCard(session) {
  const active = state.view === "workspace" && session.id === state.selectedSessionId;
  const canDelete = session.registrationEnabled !== false && session.mode === "editable";
  const attached = Boolean(session.attachedToCurrent);
  const assistantSession = currentAgentSession();
  const assistantAvailable = Boolean(assistantSession);
  const assistantLabel = assistantSessionLabel(assistantSession);
  const destructiveLabel = workspaceDestructiveLabel(session);
  return `
    <div class="session-card ${active ? "active" : ""} ${attached ? "attached" : "unattached"}">
      <button class="session-main" data-session-id="${escapeHtml(session.id)}" type="button">
        <strong>${escapeHtml(session.title)}</strong>
        <span>${escapeHtml(workspaceSubtitle(session))}</span>
        ${workspaceBadges(session)}
      </button>
      ${(assistantAvailable || canDelete) ? `
        <details class="session-card-more">
          <summary aria-label="Actions for ${escapeHtml(session.title)}">Actions</summary>
          <div class="session-card-actions">
            ${assistantAvailable ? attached
              ? `<button class="ghost-button compact-action" data-close-workspace-id="${escapeHtml(session.id)}" type="button">Remove from ${escapeHtml(assistantLabel)}</button>`
              : `<button class="ghost-button compact-action" data-attach-workspace-id="${escapeHtml(session.id)}" type="button">Ask in ${escapeHtml(assistantLabel)}</button>`
            : ""}
            ${canDelete ? `<button class="ghost-button compact-action" data-delete-workspace-id="${escapeHtml(session.id)}" type="button">${escapeHtml(destructiveLabel)}</button>` : ""}
          </div>
        </details>
      ` : ""}
    </div>
  `;
}

function workspaceSubtitle(session) {
  return workspaceStorageLabel(session);
}

function workspaceBadges(session) {
  if (session.kind === "run" || session.sourceType === "run") {
    return '<span class="workspace-badges"><span class="tag">run evidence</span></span>';
  }
  if (session.sourceType === "catalog" || session.mode === "read-only") {
    const label = session.kind && session.kind !== "catalog" ? `Catalog ${fieldLabel(session.kind)}` : "Catalog item";
    return `<span class="workspace-badges"><span class="tag">${escapeHtml(label)}</span></span>`;
  }
  const catalog = workspaceCatalogStatus(session);
  const catalogTitle = catalog === "Published version in Catalog"
    ? "A version was published to Catalog. Later Workspace edits do not change that published version."
    : catalog === "Based on a Catalog version"
    ? "This Workspace started from a Catalog version. Editing it does not change the original."
    : "No version of this Workspace has been published to Catalog.";
  const agentSession = currentAgentSession();
  const assistant = agentSession
    && attachedWorkspaceIds(agentSession.id).includes(session.id)
    ? `<span class="tag" title="This named Assistant conversation can use the Workspace files.">${escapeHtml(workspaceAssistantAccessLabel(session))}</span>`
    : "";
  return `
    <span class="workspace-badges">
      <span class="tag" title="${escapeHtml(catalogTitle)}">Catalog · ${escapeHtml(workspaceCatalogBadgeLabel(catalog))}</span>
      ${assistant}
    </span>
  `;
}

function workspaceCatalogBadgeLabel(status) {
  if (status === "Published version in Catalog") return "Published version";
  if (status === "Based on a Catalog version") return "Based on version";
  return "Not published";
}

function workspaceStorageLabelFromRecord(workspace) {
  const ownership = String(workspace && workspace.ownership || "");
  if (ownership === "external-reference") return "Linked local folder";
  if (
    ownership === "realm-managed"
    || ownership === "studio-owned"
    || workspace && workspace.managed_by_studio
  ) {
    return "Stored by OptPilot";
  }
  return "Workspace files";
}

function workspaceStorageLabel(session) {
  if (!session) return "Workspace files";
  if (session.ownership === "external-reference") return "Linked local folder";
  if (session.realmManaged || session.managedByStudio) return "Stored by OptPilot";
  return "Workspace files";
}

function workspaceCatalogStatusFromRecord(workspace) {
  return workspaceCatalogStatusFromValues(
    workspace && workspace.catalog_publications,
    workspace && workspace.catalog_origin,
  );
}

function workspaceCatalogStatus(session) {
  return workspaceCatalogStatusFromValues(
    session && session.catalogPublications,
    session && session.catalogOrigin,
  );
}

function workspaceCatalogStatusFromValues(publications, origin) {
  if (Array.isArray(publications) && publications.length) {
    return "Published version in Catalog";
  }
  if (
    origin
    && typeof origin === "object"
    && !Array.isArray(origin)
    && Object.keys(origin).length
  ) {
    return "Based on a Catalog version";
  }
  return "Not published to Catalog";
}

function assistantSessionLabel(session = currentAgentSession()) {
  const title = String(session && session.title || "Assistant conversation");
  if (title === "Main Session") return "Main conversation";
  const generated = title.match(/^Session (\d+)$/);
  return generated ? `Conversation ${generated[1]}` : title;
}

function workspaceAssistantAccessLabel(session) {
  const agentSession = currentAgentSession();
  if (!agentSession) return "No Assistant conversation selected";
  return attachedWorkspaceIds(agentSession.id).includes(session && session.id)
    ? `Available to ${assistantSessionLabel(agentSession)}`
    : `Not available to ${assistantSessionLabel(agentSession)}`;
}

function agentSessionCard(session) {
  const attachedCount = attachedWorkspaceIds(session.id).length;
  const status = assistantSessionStatus(session);
  const statusLabel = status === "awaiting_user_approval"
    ? "paused for approval"
    : status === "approval_forwarding_failed"
      ? "result send failed"
      : status && status !== "idle" ? status.replaceAll("_", " ") : "";
  return `
    <button class="agent-session-card ${session.id === state.selectedAgentSessionId ? "active" : ""}" data-agent-session-id="${escapeHtml(session.id)}" type="button">
      <strong>${escapeHtml(assistantSessionLabel(session))}</strong>
      <span>${escapeHtml(session.description || "Conversation")}</span>
      <span class="path-text">${attachedCount} Workspace${attachedCount === 1 ? "" : "s"} available${statusLabel ? ` - ${escapeHtml(statusLabel)}` : ""}</span>
    </button>
  `;
}

function componentButton(component) {
  const item = component.entry;
  const summary = item.summary || {};
  const selected = component.key === state.selectedComponentKey;
  const version = item.version || summary.version || "";
  const purpose = component.kind === "environment"
    ? summary.candidate_format || ""
    : component.kind === "method"
    ? (summary.candidate_formats || []).join(", ")
    : resourcePurposeLabel(item);
  return `
    <button class="entity-button ${selected ? "selected" : ""}" data-component-key="${escapeHtml(component.key)}" type="button">
      <span class="entity-button-header">
        <strong>${escapeHtml(item.label)}</strong>
        <span class="catalog-kind-chip catalog-kind-${escapeHtml(component.kind)}">${escapeHtml(catalogKindLabel(component.kind, item))}</span>
      </span>
      ${item.description ? `<span class="entity-button-description">${escapeHtml(item.description)}</span>` : ""}
      <span class="tag-row">
        ${version ? `<span class="tag">v${escapeHtml(String(version).replace(/^v/i, ""))}</span>` : ""}
        ${purpose ? `<span class="tag">${escapeHtml(purpose)}</span>` : ""}
        ${(item.tags || []).slice(0, 2).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
      </span>
    </button>
  `;
}

function catalogKindLabel(kind, item) {
  if (kind === "resource") return resourcePurposeLabel(item);
  return kind;
}

function resourcePurposeLabel(item) {
  const labels = {
    generator: "Generator",
    viewer: "Viewer",
    template: "Template",
    reference: "Reference",
  };
  const declared = String(item && item.purpose || "").trim();
  return labels[declared] || "Resource";
}

function planButton(plan) {
  const selected = plan.id === state.selectedPlanId;
  const persistence = studyPersistencePresentation(plan);
  const launch = studyLaunchPresentation(plan);
  return `
    <button class="plan-button ${selected ? "selected" : ""}" data-plan-id="${escapeHtml(plan.id)}" type="button">
      <span class="entity-button-header">
        <strong>${escapeHtml(plan.title)}</strong>
        <span class="study-plan-list-status">
          ${labeledStatusPill(persistence.label, persistence.status)}
          ${labeledStatusPill(launch.label, launch.status)}
        </span>
      </span>
      <span class="tag-row">
        ${plan.metric ? `<span class="tag">${escapeHtml(plan.metric)}</span>` : ""}
        ${plan.direction ? `<span class="tag">${escapeHtml(plan.direction)}</span>` : ""}
        ${plan.maxTrials ? `<span class="tag">${escapeHtml(plan.maxTrials)} ${Number(plan.maxTrials) === 1 ? "trial" : "trials"}</span>` : ""}
      </span>
    </button>
  `;
}

function summaryCell([label, value]) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "-")}</strong></div>`;
}

function timelineItem([kind, title, text, metadata = {}]) {
  const isStatus = isStudioStatusMessage(kind, metadata);
  const isAssistantOutput = !isStatus && (kind === "assistant" || kind === "agent" || kind === "tool");
  const time = formatMessageTime(metadata);
  if (isStatus) {
    const label = metadata.source === "studio_system" ? "studio" : "studio status";
    return `
      <div class="timeline-item ${escapeHtml(kind)} studio-status">
        ${timelineMetaHtml(label, time)}
        <div class="timeline-content">
          ${title ? `<strong>${escapeHtml(title)}</strong>` : ""}
          ${text ? `<p>${escapeHtml(text)}</p>` : ""}
        </div>
      </div>
    `;
  }
  return `
    <div class="timeline-item ${escapeHtml(kind)}">
      ${timelineMetaHtml(kind, time)}
      <div class="timeline-content">${isAssistantOutput ? renderMarkdown(text) : `<p>${escapeHtml(text)}</p>`}</div>
    </div>
  `;
}

function timelineMetaHtml(label, time) {
  return `
    <div class="timeline-meta">
      <span>${escapeHtml(label)}</span>
      ${time ? `<time datetime="${escapeHtml(time.iso)}">${escapeHtml(time.label)}</time>` : ""}
    </div>
  `;
}

function formatMessageTime(metadata = {}) {
  const value = metadata.createdAt || metadata.created_at || "";
  const label = formatEventTime(value);
  return label ? { label, iso: value } : null;
}

function isStudioStatusMessage(kind, metadata = {}) {
  const source = metadata.source || "";
  if (source === "studio_ui" || source === "studio_system") return true;
  return kind === "tool" && metadata.source !== "openhands";
}

function checkRow([label, value, status]) {
  return `
    <div class="check-row">
      <div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></div>
      ${statusPill(status === "ready" ? "passed" : "review")}
    </div>
  `;
}

function previewHtml(session) {
  return `
    <div class="preview-stage">
      <div class="sim-node source">Context</div>
      <div class="sim-link link-a"></div>
      <div class="sim-node adapter">Adapter</div>
      <div class="sim-link link-b"></div>
      <div class="sim-node metric">Metric</div>
    </div>
    <div class="detail-stats compact-stats">
      <div><span>Kind</span><strong>${escapeHtml(session.kind)}</strong></div>
      <div><span>Mode</span><strong>${escapeHtml(session.mode)}</strong></div>
      <div><span>Status</span><strong>${escapeHtml(session.status)}</strong></div>
      <div><span>Tools</span><strong>${escapeHtml(session.tools.length)}</strong></div>
    </div>
  `;
}

function compatList(pairs, target) {
  if (!pairs.length) return emptyInline("No compatible entries.");
  return `<div class="compat-list">${pairs.map((pair, index) => {
    const item = target === "method" ? pair.method : pair.environment;
    const catalogEntry = catalogEntryByUid(target, item.uid) || item;
    const tags = (catalogEntry.tags || []).slice(0, 3);
    return `
      <div class="compat-item compatible">
        <div class="compat-item-header">
          <strong>${escapeHtml(item.label)}</strong>
          <button class="ghost-button compact-action" data-build-study-index="${index}" type="button">Use in Study</button>
        </div>
        <span class="tag-row">${tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</span>
      </div>
    `;
  }).join("")}</div>`;
}

function kvPanel(title, rows) {
  return `
    <section class="kv-panel">
      <h3>${escapeHtml(title)}</h3>
      <dl>${rows.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value ?? "-")}</dd></div>`).join("")}</dl>
    </section>
  `;
}

function runRow(run) {
  const rowKey = runRowKey(run);
  const runId = canonicalRunId(run) || "identity pending";
  const runLabel = run.name || runId;
  const showRunId = runLabel !== runId;
  const plannedWork = runPlannedWork(run);
  const best = runBestPrimaryValue(run);
  const bestTitle = best.available
    ? `Best comparable Candidate ${best.label}: ${String(best.value)}`
    : runOverviewBestReason(best.reason);
  return `
    <button class="run-row ${rowKey === state.selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(rowKey)}" type="button">
      <span class="run-row-main">
        <strong title="${escapeHtml(runLabel)}">${escapeHtml(runLabel)}</strong>
        ${showRunId ? `<span class="path-text" title="${escapeHtml(runId)}">${escapeHtml(runId)}</span>` : ""}
      </span>
      ${statusPill(runStatus(run))}
      <span class="run-row-meta">
        <span title="${escapeHtml(`Planned trials: ${plannedWork}`)}">Trials: ${escapeHtml(plannedWork)}</span>
        <span title="${escapeHtml(bestTitle)}">Best comparable Candidate ${escapeHtml(best.label)}: ${best.available ? formatMetric(best.value) : "not available"}</span>
      </span>
    </button>
  `;
}

function tableFromRows(rows, columns) {
  if (!rows.length) return emptyInline("No records found.");
  return `
    <div class="table-wrap embedded">
      <table>
        <thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead>
        <tbody>${rows.map((row) => `<tr>${columns.map(([key]) => `<td>${formatCell(row[key])}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function validationHtml(result) {
  const valid = Boolean(result && result.valid);
  const launch = result && result.launch;
  const launchBlocked = Boolean(launch && launch.eligible !== true);
  const errors = (result && result.errors || []).map((error) => `<li>${escapeHtml(error)}</li>`).join("");
  return `
    <div class="validation-header">
      ${statusPill(result && result.launched ? "launched" : !valid ? "invalid" : launchBlocked ? "review" : "valid")}
      ${result && result.name ? `<strong>${escapeHtml(result.name)}</strong>` : ""}
    </div>
    ${valid || result && result.launched ? `<p class="path-text">${escapeHtml(shortPath(result.path || result.job_id || ""))}</p>` : `<ul class="error-list">${errors || "<li>Validation failed.</li>"}</ul>`}
    ${launchBlocked ? `<p class="error-text">Launch unavailable (${escapeHtml(launch.code || "unsupported")}): ${escapeHtml(publicStudyLaunchReason(launch))}</p>` : ""}
    ${valid && launch && launch.eligible === true ? `<p><small>Run support check passed; Environment and Method code was not executed.</small></p>` : ""}
  `;
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function postJson(url, payload, options = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: Boolean(options.keepalive),
  });
  const json = await response.json();
  if (!response.ok && !options.tolerateError) {
    const error = new Error(json.error || `${response.status} ${response.statusText}`);
    error.payload = json;
    throw error;
  }
  return json;
}

function newRequestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (window.crypto && typeof window.crypto.getRandomValues === "function") {
    window.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

async function deleteJson(url, options = {}) {
  const response = await fetch(url, { method: "DELETE" });
  const json = await response.json();
  if (!response.ok && !options.tolerateError) throw new Error(json.error || `${response.status} ${response.statusText}`);
  return json;
}

function statusPill(status) {
  return `<span class="status-pill ${statusClass(status)}">${escapeHtml(status || "unknown")}</span>`;
}

function capabilityItem(capability) {
  const label = typeof capability === "string" ? capability : capability && capability.label || "Capability";
  const status = typeof capability === "object" && capability ? capability.status || "available" : "available";
  return `
    <div class="capability-item">
      <span>${escapeHtml(label)}</span>
      ${statusPill(status)}
    </div>
  `;
}

function statusClass(status) {
  const value = String(status || "unknown");
  if (["success", "succeeded", "completed", "compatible", "ready", "valid", "schema valid", "launched", "passed", "editable", "registered", "available", "saved", "connected", "configured", "published", "docker", "podman"].includes(value)) return "status-ready";
  if (["failed", "cancelled", "invalid", "incompatible", "unavailable", "offline", "missing", "off", "setup"].includes(value)) return "status-failed";
  if (["running", "validating", "opening", "sealing", "preparing"].includes(value)) return "status-running";
  if (["review", "draft", "read-only", "idle", "optional", "host", "chat", "limited", "pending"].includes(value)) return "status-review";
  return `status-${escapeHtml(value)}`;
}

function formatCell(value) {
  if (value == null || value === "") return "-";
  if (typeof value === "object") return `<pre class="inline-json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
  return escapeHtml(String(value));
}

function formatMetric(value) {
  if (value == null || value === "") return "-";
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric.toFixed(Math.abs(numeric) >= 100 ? 1 : 4).replace(/\.?0+$/, "");
  return escapeHtml(String(value));
}

function positiveOptionalNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

function nonNegativeOptionalNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function yamlScalar(value) {
  return JSON.stringify(String(value ?? ""));
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function shortPath(path) {
  if (!path) return "";
  const cwd = state.workspace && state.workspace.cwd;
  return cwd && String(path).startsWith(cwd) ? String(path).slice(cwd.length + 1) : String(path);
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0);
}

function slug(value) {
  return String(value || "item").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "item";
}

function emptyInline(message) {
  return `<div class="empty-inline">${escapeHtml(message)}</div>`;
}

function emptyState(message) {
  return `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function renderMarkdown(value) {
  const text = normalizeAssistantMarkdown(value);
  if (!text) return "";
  const lines = text.split("\n");
  const html = [];
  let paragraph = [];
  let listItems = [];
  let listTag = "ul";
  let codeLines = [];
  let inCode = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<${listTag}>${listItems.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</${listTag}>`);
    listItems = [];
    listTag = "ul";
  };

  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index];
    const line = rawLine.trimEnd();
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushParagraph();
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }
    if (isMarkdownTableStart(lines, index)) {
      flushParagraph();
      flushList();
      const rendered = renderMarkdownTable(lines, index);
      html.push(rendered.html);
      index = rendered.nextIndex - 1;
      continue;
    }
    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(heading[1].length + 2, 5);
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    if (/^[-*_]{3,}$/.test(trimmed)) {
      flushParagraph();
      flushList();
      html.push("<hr>");
      continue;
    }
    const unordered = trimmed.match(/^[-*]\s+(.+)$/);
    const ordered = trimmed.match(/^\d+\.\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const nextTag = ordered ? "ol" : "ul";
      if (listItems.length && listTag !== nextTag) flushList();
      listTag = nextTag;
      listItems.push((unordered || ordered)[1]);
      continue;
    }
    flushList();
    paragraph.push(trimmed);
  }
  if (inCode) html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  flushParagraph();
  flushList();
  return html.join("");
}

function normalizeAssistantMarkdown(value) {
  let text = String(value ?? "").replace(/\r\n/g, "\n").trim();
  if (!text) return "";
  text = text.replace(/[ \t]+---[ \t]+/g, "\n\n---\n\n");
  text = text.replace(/[ \t]+(#{1,4})[ \t]+/g, "\n\n$1 ");
  text = text.replace(/[ \t]+([-*])[ \t]+(?=(?:\*\*)?[A-Za-z0-9])/g, "\n$1 ");
  text = text.replace(/[ \t]+(\d+\.)[ \t]+(?=(?:\*\*)?[A-Za-z0-9])/g, "\n$1 ");
  text = normalizeCollapsedMarkdownTables(text);
  return text;
}

function normalizeCollapsedMarkdownTables(text) {
  return text.split("\n").map((line) => {
    if (!line.includes("||")) return line;
    if (!/\|\|\s*:?-{3,}:?/.test(line) && !/:?-{3,}:?\s*\|\|/.test(line)) return line;
    return line.replace(/\|\|/g, "|\n|");
  }).join("\n");
}

function isMarkdownTableStart(lines, index) {
  if (index + 1 >= lines.length) return false;
  const header = lines[index].trim();
  const divider = lines[index + 1].trim();
  return splitMarkdownTableRow(header).length > 1 && isMarkdownTableDivider(divider);
}

function renderMarkdownTable(lines, startIndex) {
  const headers = splitMarkdownTableRow(lines[startIndex]);
  const rows = [];
  let nextIndex = startIndex + 2;
  while (nextIndex < lines.length) {
    const line = lines[nextIndex].trim();
    if (!line || splitMarkdownTableRow(line).length < 2 || isMarkdownTableDivider(line)) break;
    rows.push(splitMarkdownTableRow(line));
    nextIndex += 1;
  }
  return {
    nextIndex,
    html: `
      <div class="markdown-table-wrap">
        <table>
          <thead><tr>${headers.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead>
          <tbody>${rows.map((row) => `<tr>${headers.map((_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("")}</tbody>
        </table>
      </div>
    `,
  };
}

function splitMarkdownTableRow(line) {
  let value = String(line || "").trim();
  if (!value.includes("|")) return [];
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  return value.split("|").map((cell) => cell.trim());
}

function isMarkdownTableDivider(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(value ?? ""));
  }
  return String(value ?? "").replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}
