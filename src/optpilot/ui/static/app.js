const STORAGE_KEYS = {
  selectedAgentSessionId: "optpilot.studio.selectedAgentSessionId",
};

const state = {
  view: "workspace",
  workspace: null,
  runtime: null,
  codeServer: null,
  uiWorkspaces: [],
  catalog: { environments: [], methods: [], studies: [], resources: [] },
  compatibility: { pairs: [] },
  runs: [],
  jobs: [],
  sessions: [],
  agentSessions: [],
  selectedAgentSessionId: loadStoredValue(STORAGE_KEYS.selectedAgentSessionId),
  agentWorkspaceAttachments: {},
  selectedWorkspaceByAgentSession: {},
  assistantMessagesBySession: {},
  agentApprovalsBySession: {},
  agentEventsBySession: {},
  handledPreviewEventIds: new Set(),
  cancellingAgentSessionIds: new Set(),
  agentSessionSeq: 1,
  plans: [],
  selectedSessionId: null,
  selectedFileKey: null,
  selectedComponentKey: null,
  componentFilter: "all",
  componentSearch: "",
  planSearch: "",
  selectedPlanId: null,
  selectedRunId: null,
  selectedRun: null,
  runStatusFilter: "all",
  activeRunTab: "overview",
  sessionTab: "terminal",
  workbenchMode: "code",
  assistantOpen: false,
  assistantMode: "chat",
  assistantPanelWidth: 320,
  registrationDraft: null,
  embeddedCodeUrl: "",
  embeddedCodeFolder: "",
  workspacePreviews: {},
  interfaceLaunch: null,
  platformReady: false,
  codeWorkspaceStatus: "idle",
  codeWorkspaceMessage: "",
  codeWorkspacePaused: false,
  pendingJobId: null,
  agentSettings: null,
  agentRuntimeStatus: null,
  settingsOpen: false,
  pendingWorkspaceCleanup: null,
};

const els = {};

function loadStoredValue(key) {
  try {
    return window.localStorage.getItem(key) || null;
  } catch (error) {
    return null;
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

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  loadAll();
  setInterval(loadRunsAndJobs, 3000);
  setInterval(syncActiveAgentSession, 5000);
  setInterval(refreshPlatformStatus, 6000);
});

function cacheElements() {
  for (const id of [
    "healthStatus",
    "sidebarCodeServer",
    "sidebarServiceStatus",
    "assistantSettingsButton",
    "pageTitle",
    "pageSubtitle",
    "refreshButton",
    "newSessionButton",
    "assistantToggleButton",
    "assistantBackButton",
    "assistantTitle",
    "assistantSubtitle",
    "assistantSessionList",
    "assistantSessionCards",
    "assistantContextHint",
    "assistantResizeHandle",
    "closeAssistantButton",
    "workspaceTitleInput",
    "openWorkspaceExternalButton",
    "primaryActionButton",
    "sessionCount",
    "sessionList",
    "newWorkspaceButton",
    "sessionTitle",
    "sessionPath",
    "sessionStatus",
    "sessionSummary",
    "sessionFiles",
    "sessionContext",
    "sessionTools",
    "sessionWorkspaceActions",
    "codeWorkbench",
    "previewWorkbench",
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
    "openWorkspacePreviewButton",
    "reloadWorkspacePreviewButton",
    "agentTimeline",
    "agentInput",
    "sendAgentButton",
    "sessionBottom",
    "componentList",
    "componentDetail",
    "planList",
    "planDetail",
    "totalRuns",
    "runningRuns",
    "completedTrials",
    "failureCount",
    "runFilter",
    "componentSearch",
    "planSearch",
    "runsTable",
    "runDetail",
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
    "workspaceCleanupRegisterButton",
    "workspaceCleanupDeleteButton",
  ]) {
    els[id] = document.getElementById(id);
  }
}

function bindEvents() {
  const on = (element, eventName, handler) => {
    if (element) element.addEventListener(eventName, handler);
  };
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
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
        openCodeServerEmbedded();
      } else {
        setWorkbenchMode(button.dataset.workbenchMode);
      }
    });
  });
  on(els.refreshButton, "click", loadAll);
  on(els.assistantSettingsButton, "click", openSettings);
  on(els.settingsCloseButton, "click", closeSettings);
  on(els.settingsCancelButton, "click", closeSettings);
  on(els.settingsSaveButton, "click", saveSettings);
  on(els.settingsModal, "click", (event) => {
    if (event.target === els.settingsModal) closeSettings();
  });
  on(els.workspaceCleanupKeepButton, "click", keepPendingWorkspaceDraft);
  on(els.workspaceCleanupRegisterButton, "click", registerPendingWorkspaceDraft);
  on(els.workspaceCleanupDeleteButton, "click", deletePendingWorkspaceDraft);
  on(els.workspaceCleanupModal, "click", (event) => {
    if (event.target === els.workspaceCleanupModal) keepPendingWorkspaceDraft();
  });
  on(els.newSessionButton, "click", createAgentSession);
  on(els.newWorkspaceButton, "click", createBlankSession);
  on(els.assistantToggleButton, "click", toggleAssistant);
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
  on(els.openWorkspaceExternalButton, "click", openActiveWorkspaceExternal);
  on(els.startEmbeddedCodeButton, "click", startCodeWorkspaceFromUser);
  on(els.reloadEmbeddedCodeButton, "click", reloadEmbeddedCodeWorkspace);
  on(els.pauseCodeWorkspaceButton, "click", stopCodeServer);
  on(els.workspacePreviewPort, "input", updateWorkspacePreviewPort);
  on(els.openWorkspacePreviewButton, "click", openWorkspacePreview);
  on(els.reloadWorkspacePreviewButton, "click", reloadWorkspacePreview);
  on(els.primaryActionButton, "click", primaryAction);
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
  on(els.planSearch, "input", () => {
    state.planSearch = els.planSearch.value;
    renderExperiments();
  });
}

async function loadAll() {
  await Promise.all([loadWorkspace(), loadRuntimeHealth(), loadCodeServerStatus(), loadAgentSettings(), loadCatalogAndCompatibility(), loadUiWorkspaces(), loadAgentSessions(), loadRunsAndJobs()]);
  rebuildDerivedState();
  renderAll();
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

async function loadCatalogAndCompatibility() {
  const [catalog, compatibility] = await Promise.all([getJson("/api/catalog"), getJson("/api/compatibility")]);
  state.catalog = catalog;
  state.compatibility = compatibility;
}

async function loadUiWorkspaces() {
  try {
    const payload = await getJson("/api/workspaces");
    state.uiWorkspaces = payload.workspaces || [];
  } catch (error) {
    state.uiWorkspaces = [];
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
  let runsPayload;
  let jobsPayload;
  try {
    [runsPayload, jobsPayload] = await Promise.all([getJson("/api/runs"), getJson("/api/jobs")]);
  } catch (error) {
    return;
  }
  state.runs = runsPayload.runs || [];
  state.jobs = jobsPayload.jobs || [];
  if (state.pendingJobId) {
    const job = state.jobs.find((item) => item.job_id === state.pendingJobId);
    if (job && job.run_dir) {
      const run = state.runs.find((item) => item.path === job.run_dir);
      if (run) {
        state.pendingJobId = null;
        await loadRunDetail(run.id, { keepTab: true });
        setView("runs");
      }
    }
  }
  if (!state.selectedRunId && state.runs[0]) state.selectedRunId = state.runs[0].id;
  if (state.selectedRunId && state.view === "runs" && (!state.selectedRun || state.selectedRun.run && state.selectedRun.run.id !== state.selectedRunId)) {
    loadRunDetail(state.selectedRunId, { keepTab: true, skipListRender: true });
  }
  if (state.view === "runs") renderRuns();
}

function rebuildDerivedState() {
  const previousSessionId = state.selectedSessionId;
  const previousPlanId = state.selectedPlanId;
  state.sessions = buildSessions();
  state.plans = buildPlans();
  ensureAgentSessions();
  const attachedIds = attachedWorkspaceIds();
  const agentSelectedWorkspace = state.selectedWorkspaceByAgentSession[state.selectedAgentSessionId];
  state.selectedSessionId = state.view === "workspace"
    ? attachedIds.includes(agentSelectedWorkspace)
      ? agentSelectedWorkspace
      : attachedIds.includes(previousSessionId)
      ? previousSessionId
      : attachedIds[0] || null
    : null;
  if (currentAgentSession()) state.selectedWorkspaceByAgentSession[state.selectedAgentSessionId] = state.selectedSessionId;
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
      title: "Main Session",
      description: "General OptPilot work",
      createdAt: "now",
    };
    state.agentSessions = [session];
    state.selectedAgentSessionId = session.id;
    storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
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
  state.selectedAgentSessionId = (withWorkspaces || state.agentSessions[0] || {}).id || null;
  storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
}

function defaultAssistantMessages() {
  return [["assistant", "Ready", "I can use the current page, attached workspace roots, catalog, study plans, runs, and Code Server context.", {
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

function currentAssistantMessages() {
  const session = currentAgentSession();
  if (!session) return defaultAssistantMessages();
  if (!state.assistantMessagesBySession[session.id]) state.assistantMessagesBySession[session.id] = defaultAssistantMessages();
  return state.assistantMessagesBySession[session.id];
}

function currentAssistantApprovals() {
  const session = currentAgentSession();
  if (!session) return [];
  const seen = new Set();
  return (state.agentApprovalsBySession[session.id] || []).filter((approval) => {
    if (approval.status !== "pending") return false;
    const key = approvalDisplayKey(approval);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function approvalDisplayKey(approval) {
  if (!approval || typeof approval !== "object") return "";
  if (approval.request_key) return String(approval.request_key);
  const args = approval.arguments && typeof approval.arguments === "object" ? { ...approval.arguments } : {};
  delete args._openhands_tool_call_id;
  delete args.approved;
  return stableJsonStringify({
    tool: approval.tool || "",
    kind: approval.kind || "",
    title: approval.title || "",
    summary: approval.summary || "",
    targets: approval.targets || [],
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
  if (!["waiting_for_agent", "running"].includes(session.status || "")) return;
  try {
    const payload = await postJson(`/api/agent-sessions/${encodeURIComponent(session.id)}/sync`, {});
    if (payload.session) {
      await updateAgentSessionFromPayload(payload.session);
    }
  } catch (error) {
    // Keep the transcript stable; the next poll or refresh can retry.
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
    : state.runs.find((run) => run.id === state.selectedRunId);
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
      path: component.entry.path,
    } : null,
    selected_study_plan: isStudiesPage && plan ? {
      id: plan.id,
      title: plan.title,
      source: plan.source,
      status: plan.status,
      study_path: plan.study && plan.study.path || plan.draft && plan.draft.path || "",
      environment_id: plan.environment && plan.environment.id || "",
      method_id: plan.method && plan.method.id || "",
    } : null,
    selected_run: isRunsPage && selectedRun ? {
      id: selectedRun.id,
      name: selectedRun.name,
      path: selectedRun.path,
      status: selectedRun.status,
      method_id: selectedRun.method && selectedRun.method.id || "",
      environment_id: selectedRun.environment_id || "",
    } : null,
    registration_menu: isRegistrationMode && state.registrationDraft ? {
      workspace_id: state.registrationDraft.backendWorkspaceId || state.registrationDraft.workspaceId,
      status: state.registrationDraft.status,
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
    activate: ["waiting_for_agent", "running"].includes(session && session.status || ""),
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
  await loadUiWorkspaces();
  rebuildDerivedState();
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
    .map((session) => ({ ...session, attachedToCurrent: attached.has(session.id) }))
    .sort((left, right) => {
      if (left.attachedToCurrent !== right.attachedToCurrent) return left.attachedToCurrent ? -1 : 1;
      return workspaceSortMs(right.updatedAt || right.createdAt) - workspaceSortMs(left.updatedAt || left.createdAt);
    });
}

function workspaceSortMs(value) {
  const parsed = timestampMs(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

async function attachWorkspaceToCurrent(workspaceId) {
  const agentSession = currentAgentSession();
  if (!agentSession || !workspaceId) return;
  const workspace = state.sessions.find((item) => item.id === workspaceId);
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
  state.selectedSessionId = workspaceId || null;
  if (state.selectedAgentSessionId) {
    state.selectedWorkspaceByAgentSession[state.selectedAgentSessionId] = workspaceId || null;
  }
  if (options.sync) syncSelectedWorkspaceToBackend(workspaceId || "");
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
  renderWorkspace();
  renderCatalog();
  renderExperiments();
  renderRuns();
  renderAssistant();
  renderSettingsModal();
  renderWorkspaceCleanupModal();
  if (state.selectedRunId && state.view === "runs") {
    loadRunDetail(state.selectedRunId, { keepTab: true });
  }
}

async function openSettings() {
  state.settingsOpen = true;
  await loadAgentSettings();
  fillSettingsForm();
  renderSettingsModal();
}

function closeSettings() {
  state.settingsOpen = false;
  renderSettingsModal();
}

function renderSettingsModal() {
  if (!els.settingsModal) return;
  els.settingsModal.hidden = !state.settingsOpen;
  document.body.classList.toggle("settings-open", state.settingsOpen);
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
    els.assistantLauncherSubtitle.textContent = assistantRuntimeLabel(status);
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
  }
  fillSettingsForm();
  renderSettingsModal();
  renderPlatformStatus();
  renderAssistant();
}

function setView(view) {
  if (view !== "workspace") {
    clearSelectedWorkspaceForPage();
  } else if (!state.selectedSessionId) {
    const firstAttached = attachedWorkspaceIds()[0] || null;
    if (firstAttached) setSelectedWorkspace(firstAttached, { sync: true });
  }
  state.view = view;
  renderNavigation();
  if (view === "workspace") renderWorkspace();
  if (view !== "workspace") renderWorkspace();
  if (view === "catalog") renderCatalog();
  if (view === "experiments") renderExperiments();
  if (view === "runs") {
    renderRuns();
    if (state.selectedRunId && (!state.selectedRun || state.selectedRun.run && state.selectedRun.run.id !== state.selectedRunId)) {
      loadRunDetail(state.selectedRunId, { keepTab: true, skipListRender: true });
    }
  }
  renderAssistant();
}

function setWorkbenchMode(mode) {
  state.workbenchMode = mode === "preview" ? "preview" : "code";
  renderWorkbenchMode();
  if (state.workbenchMode === "preview") renderPreviewWorkbench();
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
  if (els.assistantBackButton) els.assistantBackButton.hidden = isSessionList;
  if (els.assistantTitle) {
    els.assistantTitle.textContent = isSessionList ? "Assistant Sessions" : isRegistration ? "Register to Catalog" : session ? session.title : "OptPilot Assistant";
  }
  if (els.assistantSubtitle) {
    els.assistantSubtitle.textContent = isSessionList
      ? "Resume a conversation or start a new one"
      : isRegistration
        ? "Discover configs, validate targets, and register selected files"
      : "";
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
  queueAssistantStepAutoScroll();
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

function assistantTimelineHtml(session) {
  return `${assistantInterleavedTimelineHtml(session)}${assistantApprovalsHtml()}`;
}

function assistantApprovalsHtml() {
  const approvals = currentAssistantApprovals();
  if (!approvals.length) return "";
  return `
    <div class="approval-stack">
      ${approvals.map((approval) => `
        <div class="approval-card">
          <div>
            <span>${escapeHtml(approval.kind || "approval")}</span>
            <strong>${escapeHtml(approval.title || "Approval requested")}</strong>
            <p>${escapeHtml(approval.summary || "")}</p>
            ${(approval.targets || []).length ? `<small>${escapeHtml((approval.targets || []).join(" - "))}</small>` : ""}
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
    const isWorking = isLatestUserTurn && !hasAssistantReply && Boolean(session && ["waiting_for_agent", "running"].includes(session.status || ""));
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
    return { ...base, title: "Workspace attached", detail: payload.workspace_id || "" };
  }
  if (event.type === "workspace_detached") {
    return { ...base, title: "Workspace detached", detail: payload.workspace_id || "" };
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
    return { ...base, title: "Approved result sent to OpenHands", detail: payload.tool || payload.tool_call_id || "" };
  }
  if (event.type === "openhands_tool_result_forward_skipped") {
    return { ...base, title: "Approved result kept in Studio", detail: payload.reason || payload.tool || "" };
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
    return { ...base, title: "Session created", detail: payload.title || "" };
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
    if (assistantIsBusy()) return;
    sendAgentMessage();
  }
}

function updateAssistantComposerState() {
  if (!els.sendAgentButton) return;
  const busy = assistantIsBusy();
  const session = currentAgentSession();
  const cancelling = Boolean(session && state.cancellingAgentSessionIds.has(session.id));
  els.sendAgentButton.disabled = cancelling;
  els.sendAgentButton.classList.toggle("stopping", busy);
  els.sendAgentButton.setAttribute("aria-label", busy ? "Stop assistant" : "Send message");
  els.sendAgentButton.setAttribute("title", busy ? "Stop assistant" : "Send message");
  els.sendAgentButton.innerHTML = busy
    ? `<span aria-hidden="true" class="stop-icon"></span>`
    : `<span aria-hidden="true">&uarr;</span>`;
}

function assistantIsBusy() {
  const session = currentAgentSession();
  return Boolean(session && ["waiting_for_agent", "running"].includes(session.status || ""));
}

function assistantPromptForContext() {
  if (state.assistantOpen && state.assistantMode === "registration") {
    return "Help me choose, validate, and register the right config files from this workspace.";
  }
  if (state.view === "runs") {
    const runName = state.selectedRun && state.selectedRun.run && state.selectedRun.run.name;
    return runName
      ? `Summarize evidence for ${runName}, compare candidates, and explain failures or metrics.`
      : "Summarize the selected run, compare candidates, and inspect failures or artifacts.";
  }
  if (state.view === "catalog") return "Help me inspect this catalog entry or open an editable workspace.";
  if (state.view === "experiments") return "Help me configure a study plan, validate it, and prepare it for launch.";
  return "Help me inspect this workspace, edit code, validate configs, or register catalog entries.";
}

function assistantContextSummary() {
  const parts = [`Viewing ${currentViewLabel()}`];
  if (state.view === "catalog") {
    const component = componentByKey(state.selectedComponentKey);
    parts.push(component ? `Catalog entry: ${component.entry.label} (${component.kind})` : "No catalog entry selected");
  } else if (state.view === "experiments") {
    const plan = currentPlan();
    parts.push(plan ? `Study config: ${plan.title}` : "No study config selected");
  } else if (state.view === "runs") {
    const run = selectedRunSummary();
    parts.push(run ? `Run: ${run.name || run.id}${run.status ? ` (${run.status})` : ""}` : "No run selected");
  } else {
    const workspace = currentSession();
    parts.push(workspace ? `Workspace open: ${workspace.title}` : "No workspace open");
  }
  return parts.join(" · ");
}

function selectedRunSummary() {
  if (state.selectedRun && state.selectedRun.run) return state.selectedRun.run;
  return state.runs.find((run) => run.id === state.selectedRunId) || null;
}

function renderNavigation() {
  ["workspace", "catalog", "experiments", "runs"].forEach((view) => {
    document.body.classList.toggle(`view-${view}`, state.view === view);
  });
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active-view", section.id === `${state.view}View`);
  });
  const titles = {
    workspace: ["Editor", "Open, preview, and register the selected workspace."],
    catalog: ["Catalog", "Reusable environments, methods, and resources."],
    experiments: ["Studies", "Study configurations for launching optimization runs."],
    runs: ["Runs", "Run history, metrics, artifacts, and logs."],
  };
  els.pageTitle.textContent = titles[state.view][0];
  els.pageSubtitle.textContent = titles[state.view][1];
  if (els.primaryActionButton) els.primaryActionButton.textContent = state.view === "experiments" ? "Launch" : "Register to Catalog";
}

function currentViewLabel() {
  return {
    workspace: "Editor",
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
  return {
    id: workspace.id,
    backendWorkspaceId: workspace.id,
    kind: primary ? primary.kind : sourceType,
    mode: workspace.mode || "editable",
    sourceType,
    title: workspace.title || "Workspace",
    status: workspace.status || (entries.length ? "registered" : "ready"),
    target: workspace.source_path || workspace.root,
    path: shortPath(workspace.root || workspace.source_path || ""),
    ideFolder: shortPath(workspace.root || ""),
    codeFolder: workspace.root,
    context: [sourceType, workspace.description || "workspace", shortPath(workspace.root || "")].filter(Boolean),
    tools: workspaceCapabilities(workspace),
    registrationEnabled: workspace.registration_enabled !== false,
    registeredEntries: entries,
    attachedSessions: workspace.attached_sessions || [],
    ownership: workspace.ownership || (workspace.managed_by_studio ? "studio-owned" : "external-reference"),
    managedByStudio: Boolean(workspace.managed_by_studio),
    deleteAction: workspace.delete_action || (workspace.managed_by_studio ? "delete_draft" : "remove_reference"),
    deleteLabel: workspace.source_type === "catalog-copy" && workspace.managed_by_studio
      ? "Delete Copy"
      : workspace.delete_label || (workspace.managed_by_studio ? "Delete Draft" : "Remove From Studio"),
    runtime: workspace.runtime || null,
    updatedAt: workspace.updated_at || workspace.created_at || "",
    createdAt: workspace.created_at || "",
    files,
    lenses: [["Source", sourceType], ["Mode", workspace.mode || "editable"], ["Registered", entries.length ? String(entries.length) : "none"]],
    timeline: [["assistant", "Workspace attached", workspace.description || "Workspace is available to the current assistant session."]],
    terminal: workspace.registration_enabled === false
      ? ["$ optpilot inspect-run", `root: ${shortPath(workspace.root || "")}`]
      : ["$ optpilot discover-configs", `root: ${shortPath(workspace.root || "")}`],
    checks: [
      ["Workspace root", shortPath(workspace.root || ""), "ready"],
      ["Ownership", workspace.ownership || (workspace.managed_by_studio ? "studio-owned" : "external-reference"), "ready"],
      ["Runtime", workspace.runtime && workspace.runtime.status || "unavailable", workspace.runtime && workspace.runtime.containerized ? "ready" : "review"],
      ["Catalog registration", entries.length ? "registered" : "not registered", entries.length ? "ready" : "review"],
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
    { label: "Prepare registration", status: "available" },
    { label: "Open preview", status: "optional" },
  ];
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
    const environment = catalogEntryByPath("environment", summary.environmentPath) || catalogReference("environment", summary.environmentPath || summary.environment);
    const method = catalogEntryByPath("method", summary.methodPath) || catalogReference("method", summary.methodPath || summary.method);
    plans.push({
      id: `saved-${study.uid}`,
      title: study.label,
      source: shortPath(study.path),
      status: "saved",
      study,
      environment,
      method,
      metric: objective.metric || "",
      direction: objective.direction || "",
      aggregation: objective.aggregation || "mean",
      secondaryMetrics: objective.secondaryMetrics || [],
      maxTrials: budget.maxTrials || "",
      maxFailures: budget.maxFailures || "",
      backend: execution.backend || "local",
      parallelism: execution.parallelism || "",
      timeoutSeconds: execution.timeoutSeconds || "",
      evidenceLevel: evidence.level || "",
      evidenceStorage: evidence.outputFileStorage || "",
      seed: reproducibility.seed ?? "",
      checks: [],
      yaml: study.yaml || `# Saved study\n# ${shortPath(study.path)}\n`,
      draft: null,
    });
  }
  return plans;
}

function renderWorkspace() {
  const allWorkspaces = orderedWorkspaceSessions();
  const attachedCount = attachedWorkspaceIds().length;
  const session = currentSession();
  els.sessionCount.textContent = allWorkspaces.length ? `${attachedCount}/${allWorkspaces.length}` : "0";
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
  renderWorkspaceWorkbenchToolbar(session);
  els.sessionSummary.innerHTML = [
    ["Mode", session.mode],
    ["Ownership", session.ownership || "-"],
    ["Runtime", session.runtime && session.runtime.status || "-"],
    ["Target", shortPath(session.target)],
    ["IDE folder", session.ideFolder],
  ].map(summaryCell).join("");
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
    <button class="file-tree-item open-session-code" type="button">Open folder in Code Server</button>
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
  state.assistantMode = "registration";
  state.assistantOpen = true;
  pushAssistantMessage(["assistant", "Registration opened", `Prepared catalog registration for ${session.title}.`]);
  renderAssistant();
  if (!session.backendWorkspaceId) {
    pushAssistantMessage(["tool", "Registration unavailable", "This workspace has not been persisted yet. Create or reopen it before registering catalog entries."]);
    renderAssistant();
    return;
  }
  try {
    const payload = await postJson(`/api/workspaces/${encodeURIComponent(session.backendWorkspaceId)}/discover-configs`, {});
    state.registrationDraft = buildRegistrationDraft(session, payload.configs || []);
    renderAssistant();
  } catch (error) {
    pushAssistantMessage(["tool", "Config discovery failed", String(error.message || error)]);
    renderAssistant();
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
    resourceId: slug(session.title || session.id || "resource"),
    resourceDescription: session.context && session.context[1] || "",
    note: alreadyRegistered
      ? "This workspace is already registered in the catalog. Create an editable copy if you want to modify and register a new version."
      : configs.length
      ? "Select one or more configs, validate them, then register selected files to user_catalog."
      : "No environment or method config was found. You can add one in Code Server or register this workspace as a reusable resource.",
  };
}

function registrationMenuHtml() {
  const session = currentSession();
  const draft = state.registrationDraft || (session ? buildRegistrationDraft(session) : null);
  if (!draft) return emptyState("Select a workspace before registering to the catalog.");
  state.registrationDraft = draft;
  const configs = draft.configs || [];
  return `
    <div class="registration-panel">
      <div class="registration-summary">
        <span class="mini-label">Workspace</span>
        <strong>${escapeHtml(draft.workspaceTitle)}</strong>
        <p>${escapeHtml(draft.note)}</p>
      </div>
      <div class="registration-steps">
        ${registrationStep("1", "Discover configs", configs.length ? `${configs.length} candidate config${configs.length === 1 ? "" : "s"} found` : "No config discovered", configs.length ? "ready" : "review")}
        ${registrationStep("2", "Select targets", configs.filter((item) => item.selected).length ? "Targets selected" : "Choose at least one target", configs.some((item) => item.selected) ? "ready" : "review")}
        ${registrationStep("3", "Validate", validationSummary(configs), configs.every((item) => item.validation === "valid" || item.validation === "read-only source") && configs.length ? "ready" : "review")}
        ${registrationStep("4", "Register", draft.status === "applied" ? "Applied to catalog" : "Waiting for validation", draft.status === "applied" ? "ready" : "review")}
      </div>
      <div class="registration-targets">
        ${configs.map(registrationTarget).join("") || emptyInline("No config files yet.")}
      </div>
      ${resourceRegistrationHtml(draft)}
      <div class="registration-actions">
        <button class="ghost-button registration-discover" type="button">Discover configs</button>
        <button class="ghost-button registration-validate" type="button" ${configs.length ? "" : "disabled"}>Validate selected</button>
        <button class="primary-button registration-apply" type="button" ${configs.some((item) => item.validation === "valid") ? "" : "disabled"}>Register selected</button>
      </div>
    </div>
  `;
}

function resourceRegistrationHtml(draft) {
  if (!draft || draft.status === "applied") return "";
  return `
    <div class="registration-resource">
      <div>
        <strong>Register as Resource</strong>
        <p>Copy this draft into <code>user_catalog/resources/</code> as a reusable reference workspace.</p>
      </div>
      <label class="control-field">
        <span>Resource id</span>
        <input data-resource-registration-field="resourceId" type="text" value="${escapeHtml(draft.resourceId || "")}" />
      </label>
      <label class="control-field">
        <span>Description</span>
        <input data-resource-registration-field="resourceDescription" type="text" value="${escapeHtml(draft.resourceDescription || "")}" />
      </label>
      <button class="ghost-button registration-resource-apply" type="button">Register Resource</button>
    </div>
  `;
}

function registrationStep(number, title, text, status) {
  return `
    <div class="registration-step">
      <span>${escapeHtml(number)}</span>
      <div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(text)}</p></div>
      ${statusPill(status === "ready" ? "ready" : "review")}
    </div>
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
  if (configs.every((item) => item.validation === "read-only source")) return "Already registered";
  const valid = configs.filter((item) => item.validation === "valid").length;
  return valid ? `${valid} valid target${valid === 1 ? "" : "s"}` : "Not validated";
}

function bindRegistrationMenu() {
  if (state.assistantMode !== "registration") return;
  document.querySelectorAll("[data-registration-target]").forEach((input) => {
    input.addEventListener("change", () => {
      const draft = state.registrationDraft;
      if (!draft) return;
      const target = draft.configs.find((item) => item.key === input.dataset.registrationTarget);
      if (target) target.selected = input.checked;
      renderAssistant();
    });
  });
  document.querySelectorAll("[data-resource-registration-field]").forEach((input) => {
    input.addEventListener("input", () => {
      const draft = state.registrationDraft;
      if (!draft) return;
      draft[input.dataset.resourceRegistrationField] = input.value;
    });
  });
  const discover = document.querySelector(".registration-discover");
  if (discover) discover.addEventListener("click", async () => {
    const session = currentSession();
    if (!session || !session.backendWorkspaceId) return;
    try {
      const payload = await postJson(`/api/workspaces/${encodeURIComponent(session.backendWorkspaceId)}/discover-configs`, {});
      state.registrationDraft = buildRegistrationDraft(session, payload.configs || []);
    } catch (error) {
      pushAssistantMessage(["tool", "Config discovery failed", String(error.message || error)]);
    }
    renderAssistant();
  });
  const validate = document.querySelector(".registration-validate");
  if (validate) validate.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    if (!draft) return;
    const originalWorkspaceId = draft.workspaceId;
    keepWorkspaceSelected(originalWorkspaceId);
    if (!draft.backendWorkspaceId) {
      pushAssistantMessage(["tool", "Registration validation blocked", "Persist this workspace before validating catalog registration."]);
      keepWorkspaceSelected(originalWorkspaceId);
      renderAssistant();
      return;
    }
    try {
      const selectedPaths = draft.configs.filter((item) => item.selected).map((item) => item.backendPath || item.label);
      const created = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations`, { config_paths: selectedPaths });
      draft.manifestId = created.registration && created.registration.id;
      const validated = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations/${encodeURIComponent(draft.manifestId)}/validate`, {});
      const targets = validated.registration && validated.registration.targets || [];
      draft.configs.forEach((item) => {
        const target = targets.find((candidate) => candidate.config_path === (item.backendPath || item.label));
        item.validation = target && target.validation && target.validation.valid ? "valid" : "invalid";
      });
      draft.status = validated.registration && validated.registration.status || "validated";
      pushAssistantMessage(["tool", "Registration validation", "Selected configs were validated against the OptPilot authoring schema."]);
    } catch (error) {
      pushAssistantMessage(["tool", "Registration validation failed", String(error.message || error)]);
    }
    keepWorkspaceSelected(originalWorkspaceId);
    renderAssistant();
  });
  const apply = document.querySelector(".registration-apply");
  if (apply) apply.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    const session = currentSession();
    if (!draft || !session) return;
    if (!draft.backendWorkspaceId) {
      pushAssistantMessage(["tool", "Registration blocked", "Persist this workspace before registering catalog entries."]);
      renderAssistant();
      return;
    }
    try {
      if (!draft.manifestId) {
        const selectedPaths = draft.configs.filter((item) => item.selected).map((item) => item.backendPath || item.label);
        const created = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations`, { config_paths: selectedPaths });
        draft.manifestId = created.registration && created.registration.id;
      }
      const applied = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations/${encodeURIComponent(draft.manifestId)}/apply`, {});
      if (applied.workspace) {
        const refreshed = mergeUiWorkspace(applied.workspace);
        if (refreshed) Object.assign(session, refreshed);
      }
      draft.status = applied.registration && applied.registration.status || (applied.applied ? "applied" : "invalid");
      pushAssistantMessage(["assistant", applied.applied ? "Registration applied" : "Registration blocked", applied.applied ? "Selected targets were copied into the user catalog." : "Validation must pass before registration can be applied."]);
      renderWorkspace();
    } catch (error) {
      pushAssistantMessage(["tool", "Registration failed", String(error.message || error)]);
    }
    renderAssistant();
  });
  const resourceApply = document.querySelector(".registration-resource-apply");
  if (resourceApply) resourceApply.addEventListener("click", async () => {
    const draft = state.registrationDraft;
    const session = currentSession();
    if (!draft || !session || !draft.backendWorkspaceId) return;
    try {
      const created = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations`, {
        kind: "resource",
        resource_id: draft.resourceId || slug(session.title || session.id || "resource"),
        description: draft.resourceDescription || "",
      });
      draft.manifestId = created.registration && created.registration.id;
      const applied = await postJson(`/api/workspaces/${encodeURIComponent(draft.backendWorkspaceId)}/registrations/${encodeURIComponent(draft.manifestId)}/apply`, {});
      if (applied.workspace) {
        const refreshed = mergeUiWorkspace(applied.workspace);
        if (refreshed) Object.assign(session, refreshed);
      }
      draft.status = applied.registration && applied.registration.status || (applied.applied ? "applied" : "invalid");
      await loadCatalogAndCompatibility();
      pushAssistantMessage(["assistant", applied.applied ? "Resource registered" : "Resource registration blocked", applied.applied ? "The draft was copied into user_catalog/resources." : "Validation must pass before registration can be applied."]);
      renderCatalog();
      renderWorkspace();
    } catch (error) {
      pushAssistantMessage(["tool", "Resource registration failed", String(error.message || error)]);
    }
    renderAssistant();
  });
}

function renderEmptyWorkspace() {
  updateSidebarCodeServerStatus();
  els.sessionTitle.textContent = "No workspace attached";
  els.sessionPath.textContent = "Create a workspace or attach one from Catalog or Studies.";
  els.sessionStatus.textContent = "idle";
  els.sessionStatus.className = "status-pill status-review";
  els.sessionSummary.innerHTML = "";
  els.sessionFiles.innerHTML = "";
  els.sessionContext.innerHTML = "";
  els.sessionTools.innerHTML = "";
  els.sessionWorkspaceActions.innerHTML = `<button class="file-tree-item open-session-code" type="button" disabled>No code folder selected</button>`;
  state.embeddedCodeUrl = "";
  state.embeddedCodeFolder = "";
  state.codeWorkspaceStatus = "detached";
  state.codeWorkspaceMessage = "Attach or create a workspace to start editing.";
  if (els.embeddedCodeWorkspace) els.embeddedCodeWorkspace.removeAttribute("src");
  renderWorkspaceWorkbenchToolbar(null);
  renderPreviewWorkbench();
  renderWorkbenchMode();
  renderAssistant();
  renderSessionBottom();
}

async function selectSession(sessionId) {
  if (!attachedWorkspaceIds().includes(sessionId)) {
    await attachWorkspaceAndRender(sessionId);
    return;
  }
  if (state.view !== "workspace") setView("workspace");
  setSelectedWorkspace(sessionId);
  const agentSession = currentAgentSession();
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
  await attachWorkspaceToCurrent(workspaceId);
  if (state.view !== "workspace") setView("workspace");
  state.selectedFileKey = firstFileKey(currentSession());
  await loadUiWorkspaces();
  rebuildDerivedState();
  renderWorkspace();
  renderAssistant();
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

async function selectAgentSession(sessionId) {
  state.selectedAgentSessionId = sessionId;
  storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
  state.assistantMode = "chat";
  const selectedWorkspace = state.view === "workspace"
    ? state.selectedWorkspaceByAgentSession[sessionId] || attachedWorkspaceIds(sessionId)[0] || null
    : null;
  state.selectedSessionId = selectedWorkspace;
  const next = currentSession();
  state.selectedFileKey = firstFileKey(next);
  if (next && (isEmbeddedCodeWorkspaceActive() || shouldAutoOpenCodeWorkspace(next))) {
    await openCodeServerEmbedded();
    return;
  }
  renderWorkspace();
}

async function createAgentSession() {
  const currentAttachedIds = attachedWorkspaceIds();
  const attached = currentAttachedIds
    .map((workspaceId) => state.sessions.find((session) => session.id === workspaceId))
    .map((session) => session && session.backendWorkspaceId)
    .filter(Boolean);
  const selectedWorkspace = currentSession();
  const selectedWorkspaceId = selectedWorkspace && attached.includes(selectedWorkspace.backendWorkspaceId)
    ? selectedWorkspace.backendWorkspaceId
    : state.view === "workspace"
    ? attached[0] || ""
    : "";
  try {
    const payload = await postJson("/api/agent-sessions", {
      title: `Session ${state.agentSessions.length + 1}`,
      description: "New conversation",
      attached_workspace_ids: attached,
      selected_workspace_id: selectedWorkspaceId,
    });
    await updateAgentSessionFromPayload(payload.session);
    state.selectedAgentSessionId = payload.session.id;
    storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
  } catch (error) {
    const id = `agent-session-${Date.now().toString(36)}`;
    const index = state.agentSessionSeq++;
    const session = {
      id,
      title: `Session ${index}`,
      description: "New conversation",
      createdAt: "now",
    };
    state.agentSessions = [session, ...state.agentSessions];
    state.agentWorkspaceAttachments[id] = currentAttachedIds.slice();
    state.selectedWorkspaceByAgentSession[id] = state.view === "workspace" && currentAttachedIds.includes(state.selectedSessionId)
      ? state.selectedSessionId
      : null;
    state.assistantMessagesBySession[id] = defaultAssistantMessages();
    state.agentEventsBySession[id] = [];
    state.selectedAgentSessionId = id;
    storeValue(STORAGE_KEYS.selectedAgentSessionId, state.selectedAgentSessionId);
  }
  state.assistantMode = "chat";
  renderWorkspace();
  setAssistantOpen(true);
}

async function closeWorkspaceFromCurrentSession(workspaceId) {
  const workspace = state.sessions.find((item) => item.id === workspaceId);
  const label = workspace ? workspace.title : "this workspace";
  const agentSession = currentAgentSession();
  if (!agentSession) return;
  if (workspaceShouldPromptOnLastDetach(workspace, agentSession.id)) {
    state.pendingWorkspaceCleanup = { workspaceId, sessionId: agentSession.id, intent: "detach" };
    renderWorkspaceCleanupModal();
    return;
  }
  await detachWorkspaceFromSession(workspaceId, agentSession.id, { announce: true });
}

function workspaceShouldPromptOnLastDetach(workspace, sessionId) {
  if (!workspace || !sessionId) return false;
  if (workspace.registrationEnabled === false) return false;
  if (workspace.mode !== "editable") return false;
  if (!workspace.managedByStudio) return false;
  const attached = workspace.attachedSessions || [];
  return attached.length <= 1 && (!attached.length || attached.includes(sessionId));
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
  if (state.selectedSessionId === workspaceId) {
    const nextId = attachedWorkspaceIds(agentSession.id)[0] || null;
    setSelectedWorkspace(nextId);
    state.embeddedCodeUrl = "";
    state.embeddedCodeFolder = "";
    if (els.embeddedCodeWorkspace) els.embeddedCodeWorkspace.removeAttribute("src");
    if (nextId) {
      await openCodeServerEmbedded();
      return;
    }
  }
  if (options.announce) pushAssistantMessage(["tool", "Workspace detached", `${label} was detached from this assistant session. Files remain on disk.`]);
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
  const deleting = pending.intent === "delete";
  const destructiveLabel = workspaceDestructiveLabel(workspace);
  const isCatalogCopy = workspace.sourceType === "catalog-copy";
  if (els.workspaceCleanupTitle) els.workspaceCleanupTitle.textContent = `${deleting ? destructiveLabel : "Detach"} ${workspace.title}`;
  if (els.workspaceCleanupBody) {
    const destructiveDescription = workspace.managedByStudio
      ? isCatalogCopy
        ? "delete the Studio-managed copy folder without changing the original catalog entry"
        : "delete the Studio-managed draft folder"
      : "remove the workspace from Studio without deleting the referenced folder";
    const ownershipDescription = workspace.managedByStudio
      ? isCatalogCopy
        ? "a Studio-owned editable copy"
        : "a Studio-owned draft workspace"
      : "an unregistered workspace";
    els.workspaceCleanupBody.textContent = deleting
      ? `This is ${ownershipDescription}. Keep it in the workspace list, register reusable files to the catalog, or ${destructiveDescription}.`
      : `This is the last assistant session using ${ownershipDescription}. Keep it in the workspace list, register reusable files to the catalog, or ${destructiveDescription}.`;
  }
  if (els.workspaceCleanupDeleteButton) {
    els.workspaceCleanupDeleteButton.textContent = destructiveLabel;
    els.workspaceCleanupDeleteButton.title = workspace.managedByStudio
      ? isCatalogCopy
        ? "Delete this Studio-owned copy and runtime state. The original catalog entry is not changed."
        : "Delete the Studio-owned draft folder and runtime state."
      : "Remove this external folder from Studio without deleting files.";
  }
}

async function keepPendingWorkspaceDraft() {
  const pending = state.pendingWorkspaceCleanup;
  state.pendingWorkspaceCleanup = null;
  renderWorkspaceCleanupModal();
  if (!pending) return;
  await detachWorkspaceFromSession(pending.workspaceId, pending.sessionId, { announce: true });
}

async function registerPendingWorkspaceDraft() {
  const pending = state.pendingWorkspaceCleanup;
  state.pendingWorkspaceCleanup = null;
  renderWorkspaceCleanupModal();
  if (!pending) return;
  const workspaceId = pending.workspaceId;
  if (!attachedWorkspaceIds(pending.sessionId).includes(workspaceId)) {
    await attachWorkspaceToCurrent(workspaceId);
  }
  setSelectedWorkspace(workspaceId);
  await openRegistrationMenu();
}

async function deletePendingWorkspaceDraft() {
  const pending = state.pendingWorkspaceCleanup;
  state.pendingWorkspaceCleanup = null;
  renderWorkspaceCleanupModal();
  if (!pending) return;
  await detachWorkspaceFromSession(pending.workspaceId, pending.sessionId, { announce: false });
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
    const title = deleted.files_deleted ? "Workspace deleted" : "Workspace removed";
    const detail = deleted.files_deleted
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
  if (!workspace) return "Remove From Studio";
  if (workspace.managedByStudio && workspace.sourceType === "catalog-copy") return "Delete Copy";
  if (workspace.deleteLabel) return workspace.deleteLabel;
  return workspace.managedByStudio ? "Delete Draft" : "Remove From Studio";
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
  const hasLimited = services.some((service) => service.level === "review");
  const summary = requiredBlocked
    ? ["Needs setup", "failed"]
    : requiredWaiting || hasLimited
    ? ["Limited", "review"]
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
      label: "Code Server",
      badge: "running",
      level: "ready",
      detail: `Port ${status.port || 8766}${status.workspace_root ? ` - ${shortPath(status.workspace_root)}` : ""}`,
      required: true,
    };
  }
  if (status.installed || status.available) {
    return {
      label: "Code Server",
      badge: "ready",
      level: "review",
      detail: "Installed; start from Editor",
      required: true,
    };
  }
  return {
    label: "Code Server",
    badge: "missing",
    level: "failed",
    detail: status.error || status.install_hint || "code-server not installed",
    required: true,
  };
}

function openHandsService(status) {
  if (status.enabled && status.connected) {
    return {
      label: "OpenHands",
      badge: "connected",
      level: "ready",
      detail: status.model || "Agent server reachable",
      required: true,
    };
  }
  if (!status.enabled) {
    return {
      label: "OpenHands",
      badge: "off",
      level: "failed",
      detail: "Assistant runtime disabled",
      required: true,
    };
  }
  if (!status.credentials_configured) {
    return {
      label: "OpenHands",
      badge: "setup",
      level: "failed",
      detail: !status.model ? "Model missing" : "API key missing",
      required: true,
    };
  }
  if (status.server_configured) {
    return {
      label: "OpenHands",
      badge: "offline",
      level: "failed",
      detail: status.base_url || "Agent server not reachable",
      required: true,
    };
  }
  return {
    label: "OpenHands",
    badge: "chat",
    level: "review",
    detail: "No agent server URL configured",
    required: true,
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
  const mode = state.workbenchMode === "preview" ? "preview" : "code";
  state.workbenchMode = mode;
  const session = currentSession();
  const grid = document.querySelector("#workspaceView .workspace-grid");
  if (grid) {
    grid.classList.toggle("workbench-focused", true);
    grid.classList.toggle("code-focused", mode === "code");
    grid.classList.toggle("preview-focused", mode === "preview");
  }
  document.querySelectorAll("[data-workbench-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.workbenchMode === mode);
  });
  [
    ["code", els.codeWorkbench],
    ["preview", els.previewWorkbench],
  ].forEach(([key, element]) => {
    if (element) element.classList.toggle("active-workbench", key === mode);
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
  renderAssistant();
}

function renderWorkspaceWorkbenchToolbar(session = currentSession()) {
  if (els.workspaceTitleInput) {
    els.workspaceTitleInput.disabled = !session;
    els.workspaceTitleInput.placeholder = session ? "Workspace name" : "No workspace attached";
    if (document.activeElement !== els.workspaceTitleInput) {
      els.workspaceTitleInput.value = session ? session.title : "";
    }
  }
  if (els.primaryActionButton) {
    const hasRegistrations = Boolean(session && (session.registeredEntries || []).length);
    els.primaryActionButton.textContent = hasRegistrations ? "Registration Details" : "Register to Catalog";
    els.primaryActionButton.disabled = !session || session.registrationEnabled === false || session.mode === "read-only";
  }
  if (els.openWorkspaceExternalButton) {
    const mode = state.workbenchMode === "preview" ? "preview" : "code";
    const preview = currentWorkspacePreview(session);
    const openingPreview = preview.status === "opening";
    const codeOpening = state.codeWorkspaceStatus === "opening";
    els.openWorkspaceExternalButton.textContent = "Open Separate Window";
    els.openWorkspaceExternalButton.disabled = !session || (mode === "preview" ? (!preview.url || openingPreview) : codeOpening);
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
  try {
    const payload = await postJson(`/api/workspaces/${encodeURIComponent(session.backendWorkspaceId || session.id)}/rename`, { title });
    if (payload.workspace) {
      mergeUiWorkspace(payload.workspace);
      if (state.registrationDraft && state.registrationDraft.workspaceId === session.id) {
        state.registrationDraft.workspaceTitle = payload.workspace.title || title;
      }
    }
  } catch (error) {
    input.value = session.title;
    pushAssistantMessage(["tool", "Workspace rename failed", String(error.message || error)]);
    setAssistantOpen(true);
  } finally {
    input.disabled = false;
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
      "No workspace attached",
      state.codeWorkspaceMessage || "Attach or create a workspace to start editing.",
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
  const preview = currentWorkspacePreview(session);
  const hasWorkspace = Boolean(session);
  const hasPreview = Boolean(hasWorkspace && preview.url);
  const opening = preview.status === "opening";
  if (els.workspacePreviewPort && document.activeElement !== els.workspacePreviewPort) {
    els.workspacePreviewPort.value = String(preview.port || 5173);
  }
  if (els.workspacePreviewStatus) {
    const status = hasPreview
      ? `Port ${preview.port} in ${session.title}`
      : hasWorkspace
      ? `Run your app in ${session.title}, then open its port here.`
      : "Attach a workspace before opening a preview.";
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
      ? "No workspace attached"
      : opening
      ? "Opening workspace preview"
      : preview.status === "error"
      ? "Preview unavailable"
      : "Open a workspace preview";
  }
  if (els.workspacePreviewBody) {
    els.workspacePreviewBody.textContent = !hasWorkspace
      ? "Create or attach a workspace before launching a frontend preview."
      : opening
      ? `Preparing port ${preview.port || 5173} through the workspace runtime.`
      : preview.status === "error"
      ? preview.message || "The preview could not be opened."
      : "Start a frontend server in the workspace terminal, make it listen on 0.0.0.0, then enter the port here.";
  }
  if (els.openWorkspacePreviewButton) {
    els.openWorkspacePreviewButton.disabled = !hasWorkspace || opening;
    els.openWorkspacePreviewButton.textContent = opening ? "Opening..." : "Open Preview";
  }
  if (els.reloadWorkspacePreviewButton) {
    els.reloadWorkspacePreviewButton.disabled = !hasPreview || opening;
  }
  renderWorkspaceWorkbenchToolbar(session);
}

function renderSessionBottom() {
  if (state.sessionTab === "preview") state.sessionTab = "terminal";
  document.querySelectorAll("[data-session-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.sessionTab === state.sessionTab);
  });
  const session = currentSession();
  if (!session) {
    els.sessionBottom.innerHTML = emptyState("No workspace is attached to this assistant session.");
    return;
  }
  const content = {
    terminal: `<pre class="code-box terminal-box">${escapeHtml((session.terminal || []).join("\n"))}</pre>`,
    checks: `<div class="check-list">${(session.checks || []).map(checkRow).join("")}</div>`,
    diff: `<pre class="code-box terminal-box">--- catalog/source\n+++ ${escapeHtml(session.path)}\n@@\n+ changes stay in the workspace until registration or launch\n</pre>`,
  };
  els.sessionBottom.innerHTML = content[state.sessionTab] || content.terminal;
}

function renderCatalog() {
  if (els.componentSearch && els.componentSearch.value !== state.componentSearch) {
    els.componentSearch.value = state.componentSearch;
  }
  document.querySelectorAll("[data-component-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.componentFilter === state.componentFilter);
  });
  const query = normalizeSearch(state.componentSearch);
  const components = allComponents().filter((item) => {
    const matchesFilter = state.componentFilter === "all" || item.kind === state.componentFilter;
    const matchesSearch = !query || catalogSearchText(item).includes(query);
    return matchesFilter && matchesSearch;
  });
  if (!components.some((item) => item.key === state.selectedComponentKey)) {
    state.selectedComponentKey = components[0] && components[0].key;
  }
  els.componentList.innerHTML = components.map(componentButton).join("") || emptyInline("No catalog entries match.");
  document.querySelectorAll("[data-component-key]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedComponentKey = button.dataset.componentKey;
      renderCatalog();
    });
  });
  renderComponentDetail();
}

function renderComponentDetail() {
  const component = componentByKey(state.selectedComponentKey);
  if (!component) {
    els.componentDetail.innerHTML = emptyState("Select a catalog entry.");
    return;
  }
  const item = component.entry;
  const summary = item.summary || {};
  const hasInterface = Boolean(item.interface && item.interface.port && item.interface.command && item.interface.command.length);
  const launchState = hasInterface && state.interfaceLaunch && state.interfaceLaunch.key === componentLaunchKey(component)
    ? state.interfaceLaunch
    : null;
  const interfaceAction = hasInterface
    ? `<button class="ghost-button component-launch-interface" type="button" ${launchState ? "disabled" : ""}>${launchState ? "Launching..." : "Launch Interface"}</button>`
    : "";
  const launchStatus = launchState ? interfaceLaunchStatus(component, launchState) : "";
  if (component.kind === "resource") {
    els.componentDetail.innerHTML = `
      ${entityHeader(item, component.kind)}
      <div class="action-row">
        <button class="ghost-button component-inspect" type="button">Inspect</button>
        <button class="ghost-button component-edit" type="button">Edit Copy</button>
        ${interfaceAction}
      </div>
      ${launchStatus}
      <div class="detail-grid">
        ${kvPanel("Resource", [
          ["Files", summary.file_count ?? "-"],
          ["README", summary.readme || "-"],
          ["Mode", "read-only catalog asset"],
        ])}
        ${kvPanel("Use", [
          ["Assistant", "reference workspace"],
          ["Registration", "editable copies only"],
          ["Interface", hasInterface ? `port ${item.interface.port}` : "not declared"],
        ])}
      </div>
    `;
    els.componentDetail.querySelector(".component-inspect").addEventListener("click", () => openComponentSession(component, "inspect"));
    els.componentDetail.querySelector(".component-edit").addEventListener("click", () => openComponentSession(component, "edit"));
    const launchButton = els.componentDetail.querySelector(".component-launch-interface");
    if (launchButton) launchButton.addEventListener("click", () => launchComponentInterface(component));
    return;
  }
  const pairs = component.kind === "environment"
    ? compatibleMethodsForEnvironment(item.uid)
    : compatibleEnvironmentsForMethod(item.uid);
  els.componentDetail.innerHTML = `
    ${entityHeader(item, component.kind)}
    <div class="action-row">
      <button class="ghost-button component-inspect" type="button">Inspect</button>
      <button class="ghost-button component-edit" type="button">Edit Copy</button>
      ${interfaceAction}
    </div>
    ${launchStatus}
    <div class="detail-grid">
      ${kvPanel("Contract", component.kind === "environment" ? [
        ["Candidate", summary.candidate_format],
        ["Metrics", (summary.metrics || []).join(", ") || "-"],
        ["Evaluator", summary.evaluate_type],
      ] : [
        ["Accepts", (summary.candidate_formats || []).join(", ") || "-"],
        ["Protocol", summary.protocol],
        ["Implementation", summary.implementation_type],
      ])}
      ${kvPanel("Runtime", component.kind === "environment" ? [
        ["Timeout", summary.runtime && summary.runtime.timeoutSeconds],
        ["Sandbox", summary.runtime && summary.runtime.sandbox],
        ["Interface", hasInterface ? `port ${item.interface.port}` : "not declared"],
      ] : [
        ["Runtime", summary.runtime && summary.runtime.type],
        ["Image", summary.runtime && summary.runtime.image],
        ["Interface", hasInterface ? `port ${item.interface.port}` : "not declared"],
      ])}
    </div>
    <div class="panel-section">
      <h3>Compatible ${component.kind === "environment" ? "Methods" : "Environments"}</h3>
      ${compatList(pairs, component.kind === "environment" ? "method" : "environment")}
    </div>
  `;
  els.componentDetail.querySelector(".component-inspect").addEventListener("click", () => openComponentSession(component, "inspect"));
  els.componentDetail.querySelector(".component-edit").addEventListener("click", () => openComponentSession(component, "edit"));
  const launchButton = els.componentDetail.querySelector(".component-launch-interface");
  if (launchButton) launchButton.addEventListener("click", () => launchComponentInterface(component));
  els.componentDetail.querySelectorAll("[data-build-study-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const pair = pairs[Number(button.dataset.buildStudyIndex)];
      if (pair) createPlanFromPair(pair);
    });
  });
}

function componentLaunchKey(component) {
  if (!component) return "";
  return `${component.kind}:${component.entry && component.entry.uid || component.key}`;
}

function interfaceLaunchStatus(component, launchState) {
  const item = component.entry || {};
  const iface = item.interface || {};
  const port = iface.port || launchState.port || "-";
  const label = iface.label || launchState.label || "interface";
  const steps = (launchState.steps || []).slice(-6);
  const currentStep = steps[steps.length - 1];
  const logs = launchState.logs || {};
  const logText = [logs.stdout, logs.stderr].filter(Boolean).join("\n").trim();
  return `
    <div class="interface-launch-status" role="status" aria-live="polite">
      <span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>
      <div>
        <strong>Preparing ${escapeHtml(label)}</strong>
        <p>${escapeHtml(currentStep && currentStep.detail || `Creating an editable workspace, starting the runtime, running the launch command, and waiting for port ${port}.`)}</p>
        ${steps.length ? `
          <ol class="interface-launch-steps">
            ${steps.map((step) => `
              <li class="${escapeHtml(step.status || "running")}">
                <span>${escapeHtml(step.title || "Working")}</span>
              </li>
            `).join("")}
          </ol>
        ` : ""}
        ${logText ? `<pre class="interface-launch-log">${escapeHtml(logText)}</pre>` : ""}
      </div>
    </div>
  `;
}

function renderExperiments() {
  if (els.planSearch && els.planSearch.value !== state.planSearch) {
    els.planSearch.value = state.planSearch;
  }
  const query = normalizeSearch(state.planSearch);
  const plans = state.plans.filter((plan) => !query || planSearchText(plan).includes(query));
  els.planList.innerHTML = plans.map(planButton).join("") || emptyInline(query ? "No studies match." : "No plans yet.");
  document.querySelectorAll("[data-plan-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedPlanId = button.dataset.planId;
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
  return normalizeSearch([
    component.kind,
    component.id,
    component.path,
    component.entry && component.entry.label,
    summary.description,
    summary.goal,
    summary.candidate_format,
    summary.protocol,
    summary.implementation_type,
    component.entry && component.entry.interface && component.entry.interface.label,
    component.entry && component.entry.interface && component.entry.interface.port,
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

function renderPlanDetail() {
  const plan = currentPlan();
  if (!plan) {
    els.planDetail.innerHTML = emptyState("Select a saved study config or build a draft from Catalog.");
    return;
  }
  const draftValid = Boolean(plan.draft && plan.draft.path && (!plan.draft.validation || plan.draft.validation.valid));
  const savedConfig = Boolean(plan.study && plan.study.path);
  const launchEnabled = Boolean(savedConfig || plan.environment && plan.method);
  const launchLabel = savedConfig || draftValid ? "Launch Study" : "Save & Launch";
  const saveLabel = savedConfig ? "Save Copy" : plan.draft ? "Update Config" : "Save Config";
  const locked = !plan.environment || !plan.method;
  els.planDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(plan.title)}</h2>
        <p class="path-text">${escapeHtml(plan.source)}</p>
      </div>
      ${plan.status && plan.status !== "saved" ? statusPill(plan.status) : ""}
    </div>
    <div class="action-row">
      ${!locked ? `<button class="ghost-button plan-draft" type="button">${escapeHtml(saveLabel)}</button>` : ""}
      <button class="primary-button plan-launch" type="button" ${launchEnabled ? "" : "disabled"}>${escapeHtml(launchLabel)}</button>
    </div>
    <div class="plan-layout">
      <section class="study-config-grid">
        ${studyConfigEditor(plan, locked)}
        ${studyReadinessPanel(plan)}
      </section>
      <section>
        <h3>Study YAML</h3>
        <pre class="code-box yaml-preview">${escapeHtml(plan.draft && plan.draft.yaml || plan.yaml || planYamlPreview(plan) || "")}</pre>
        <div class="validation-box">${plan.draft ? validationHtml(plan.draft.validation || plan.draft) : ""}</div>
      </section>
    </div>
  `;
  const saveButton = els.planDetail.querySelector(".plan-draft");
  if (saveButton) saveButton.addEventListener("click", () => generatePlanDraft(plan));
  els.planDetail.querySelector(".plan-launch").addEventListener("click", () => launchPlan(plan));
  if (!locked) bindPlanConfigControls(plan);
}

function studyConfigEditor(plan, locked) {
  return `
    <section class="study-card">
      <h3>Binding</h3>
      <div class="study-binding">
        ${readonlyField("Environment", plan.environment && plan.environment.label || "-")}
        ${readonlyField("Method", plan.method && plan.method.label || "-")}
      </div>
    </section>
    <section class="study-card">
      <h3>Objective</h3>
      <div class="control-grid">
        ${selectField("Metric", "metric", plan.metric || "", metricOptions(plan), locked)}
        ${selectField("Direction", "direction", plan.direction || "maximize", ["minimize", "maximize"], locked)}
        ${selectField("Aggregation", "aggregation", plan.aggregation || "mean", ["mean", "median", "min", "max", "sum", "last", "weighted_mean"], locked)}
        ${inputField("Secondary metrics", "secondaryMetrics", (plan.secondaryMetrics || []).join(", "), "text", locked)}
      </div>
    </section>
    <section class="study-card">
      <h3>Run Policy</h3>
      <div class="control-grid">
        ${inputField("Max trials", "maxTrials", plan.maxTrials || "", "number", locked, "1")}
        ${inputField("Max failures", "maxFailures", plan.maxFailures ?? "", "number", locked, "1")}
        ${selectField("Backend", "backend", plan.backend || "local", ["local", "local_subprocess"], locked)}
        ${inputField("Parallelism", "parallelism", plan.parallelism || "", "number", locked, "1")}
        ${inputField("Timeout seconds", "timeoutSeconds", plan.timeoutSeconds || "", "number", locked, "1")}
      </div>
    </section>
    <section class="study-card">
      <h3>Evidence</h3>
      <div class="control-grid">
        ${selectField("Level", "evidenceLevel", plan.evidenceLevel || "standard", ["minimal", "standard", "full"], locked)}
        ${selectField("File storage", "evidenceStorage", plan.evidenceStorage || "reference", ["reference", "copy"], locked)}
        ${inputField("Seed", "seed", plan.seed ?? "", "number", locked, "0")}
      </div>
    </section>
  `;
}

function readonlyField(label, value) {
  return `<div class="readonly-field"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "-")}</strong></div>`;
}

function inputField(label, field, value, type = "text", disabled = false, min = "") {
  return `
    <label class="control-field">
      <span>${escapeHtml(label)}</span>
      <input data-plan-field="${escapeHtml(field)}" type="${escapeHtml(type)}" value="${escapeHtml(value ?? "")}" ${min !== "" ? `min="${escapeHtml(min)}"` : ""} ${disabled ? "disabled" : ""} />
    </label>
  `;
}

function selectField(label, field, value, options, disabled = false) {
  const optionValues = Array.from(new Set([value, ...(options || [])].filter((item) => item !== "" && item !== null && item !== undefined)));
  return `
    <label class="control-field">
      <span>${escapeHtml(label)}</span>
      <select data-plan-field="${escapeHtml(field)}" ${disabled ? "disabled" : ""}>
        ${optionValues.map((option) => `<option value="${escapeHtml(option)}" ${String(option) === String(value) ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
      </select>
    </label>
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
      refreshPlanPreview(plan);
    });
  });
}

function updatePlanField(plan, field, value) {
  if (plan.study) convertSavedPlanToDraft(plan);
  if (field === "secondaryMetrics") {
    plan.secondaryMetrics = value.split(",").map((item) => item.trim()).filter(Boolean);
  } else {
    plan[field] = value;
  }
  plan.draft = null;
  plan.status = "draft";
}

function convertSavedPlanToDraft(plan) {
  plan.originalStudy = plan.study;
  plan.study = null;
  plan.source = "draft copy";
  plan.status = "draft";
  plan.draft = null;
  plan.yaml = planYamlPreview(plan);
}

function refreshPlanPreview(plan) {
  const preview = els.planDetail.querySelector(".yaml-preview");
  if (preview) preview.textContent = planYamlPreview(plan);
  const validation = els.planDetail.querySelector(".validation-box");
  if (validation && !plan.draft) validation.innerHTML = "";
  const launchButton = els.planDetail.querySelector(".plan-launch");
  if (launchButton) launchButton.textContent = plan.draft && plan.draft.path ? "Launch Study" : "Save & Launch";
  const saveButton = els.planDetail.querySelector(".plan-draft");
  if (saveButton) saveButton.textContent = plan.draft ? "Update Config" : "Save Config";
}

function renderRuns() {
  document.querySelectorAll("[data-run-filter]").forEach((button) => {
    button.classList.toggle("active", button.dataset.runFilter === state.runStatusFilter);
  });
  const query = els.runFilter ? els.runFilter.value.trim().toLowerCase() : "";
  const rows = runRowsWithJobs();
  const runs = rows.filter((run) => {
    const matchesStatus = state.runStatusFilter === "all" || run.status === state.runStatusFilter;
    const matchesSearch = !query || runSearchText(run).includes(query);
    return matchesStatus && matchesSearch;
  });
  if (els.totalRuns) els.totalRuns.textContent = String(rows.length);
  if (els.runningRuns) els.runningRuns.textContent = String(rows.filter((run) => run.status === "running").length);
  if (els.completedTrials) els.completedTrials.textContent = String(sum(state.runs.map((run) => Number(run.completed_trials || 0))));
  if (els.failureCount) els.failureCount.textContent = String(sum(state.runs.map((run) => Number(run.failure_count || 0))));
  els.runsTable.innerHTML = runs.map(runRow).join("") || emptyInline("No runs match.");
  document.querySelectorAll(".run-row").forEach((row) => {
    row.addEventListener("click", () => loadRunDetail(row.dataset.runId));
  });
}

function runSearchText(run) {
  return `${run.name} ${run.path} ${run.status} ${run.environment_id || ""} ${run.method && run.method.id || ""}`.toLowerCase();
}

function runRowsWithJobs() {
  const runPaths = new Set(state.runs.map((run) => run.path));
  const jobRows = state.jobs
    .filter((job) => !job.run_dir || !runPaths.has(job.run_dir))
    .map((job) => ({
      id: `job:${job.job_id}`,
      name: job.study_name || job.job_id,
      path: job.run_dir || job.study_path,
      status: job.status || "running",
      method: { id: "launch job" },
      completed_trials: 0,
      best_metric: null,
      environment_id: "pending",
      job,
    }));
  return [...jobRows, ...state.runs];
}

async function loadRunDetail(runId, options = {}) {
  state.selectedRunId = runId;
  if (runId && runId.startsWith("job:")) {
    const job = state.jobs.find((item) => `job:${item.job_id}` === runId);
    state.selectedRun = job ? { job, run: jobRunSummary(job) } : null;
    if (!options.keepTab) state.activeRunTab = "overview";
    if (!options.skipListRender) renderRuns();
    renderRunDetail();
    renderAssistant();
    return;
  }
  state.selectedRun = await getJson(`/api/runs/${encodeURIComponent(runId)}`);
  if (!options.keepTab) state.activeRunTab = "overview";
  if (!options.skipListRender) renderRuns();
  renderRunDetail();
  renderAssistant();
}

function jobRunSummary(job) {
  return {
    id: `job:${job.job_id}`,
    name: job.study_name || job.job_id,
    path: job.run_dir || job.study_path,
    status: job.status || "running",
    best_metric: null,
    completed_trials: 0,
    failure_count: 0,
    method: { id: "launch job" },
    environment_id: "pending",
    objective: {},
  };
}

function renderRunDetail() {
  const detail = state.selectedRun;
  if (!detail) {
    els.runDetail.innerHTML = emptyState("Select a run to inspect observations, candidates, events, and files.");
    return;
  }
  if (detail.job && !detail.observations) {
    renderJobDetail(detail.job);
    return;
  }
  const run = detail.run;
  els.runDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(run.name)}</h2>
        <p class="path-text">${escapeHtml(shortPath(run.path))}</p>
      </div>
      <div class="detail-actions">
        ${statusPill(run.status)}
        <button class="ghost-button run-workspace" type="button">Open as Workspace</button>
      </div>
    </div>
    <div class="detail-stats">
      <div><span>Best metric</span><strong>${formatMetric(run.best_metric)}</strong></div>
      <div><span>Trials</span><strong>${escapeHtml(run.completed_trials || 0)}</strong></div>
      <div><span>Failures</span><strong>${escapeHtml(run.failure_count || 0)}</strong></div>
      <div><span>Method</span><strong>${escapeHtml(run.method && run.method.id || "-")}</strong></div>
    </div>
    ${metricChart(detail.observations || [], run.objective && run.objective.name)}
    <div class="tabs">
      ${["overview", "trials", "candidates", "events", "runtime", "files"].map((tab) => `<button class="tab ${state.activeRunTab === tab ? "active" : ""}" data-run-tab="${tab}" type="button">${tab}</button>`).join("")}
    </div>
    <div class="tab-content">${runTabContent(detail)}</div>
  `;
  document.querySelectorAll("[data-run-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeRunTab = button.dataset.runTab;
      renderRunDetail();
    });
  });
  const runWorkspaceButton = els.runDetail.querySelector(".run-workspace");
  if (runWorkspaceButton) runWorkspaceButton.addEventListener("click", () => openRunWorkspace(run.id));
  document.querySelectorAll(".file-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await getJson(`/api/runs/${encodeURIComponent(detail.run.id)}/file?path=${encodeURIComponent(button.dataset.filePath)}`);
      const preview = document.getElementById("filePreview");
      preview.innerHTML = `<h3>${escapeHtml(response.relative_path)}</h3><pre class="code-box">${escapeHtml(response.content)}</pre>`;
    });
  });
}

function renderJobDetail(job) {
  els.runDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(job.study_name || job.job_id)}</h2>
        <p class="path-text">${escapeHtml(shortPath(job.study_path))}</p>
      </div>
      <div class="detail-actions">
        ${statusPill(job.status)}
        ${job.status === "running" ? `<button class="ghost-button stop-selected-job" type="button">Stop</button>` : ""}
      </div>
    </div>
    <div class="detail-grid">
      ${kvPanel("Launch Job", [
        ["Job", job.job_id],
        ["PID", job.process_id || "-"],
        ["Exit", job.exit_code ?? "-"],
        ["Run folder", job.run_dir ? shortPath(job.run_dir) : "waiting for run directory"],
      ])}
      ${kvPanel("Logs", [
        ["stdout", shortPath(job.stdout_log || "")],
        ["stderr", shortPath(job.stderr_log || "")],
      ])}
    </div>
    <p class="path-text">This job will appear as a normal run once OptPilot writes its run directory and evidence files.</p>
  `;
  const stopButton = els.runDetail.querySelector(".stop-selected-job");
  if (stopButton) {
    stopButton.addEventListener("click", async () => {
      await postJson(`/api/jobs/${encodeURIComponent(job.job_id)}/stop`, {});
      await loadRunsAndJobs();
      await loadRunDetail(`job:${job.job_id}`, { keepTab: true });
    });
  }
}

async function openRunWorkspace(runId) {
  try {
    const agentSession = currentAgentSession();
    const payload = await postJson(`/api/runs/${encodeURIComponent(runId)}/open-workspace`, { session_id: agentSession ? agentSession.id : "" });
    if (payload.workspace) {
      const session = mergeUiWorkspace(payload.workspace);
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      await attachWorkspaceToCurrent(session.id);
      setView("workspace");
    }
  } catch (error) {
    pushAssistantMessage(["tool", "Run workspace failed", String(error.message || error)]);
    setAssistantOpen(true);
  }
}

function runTabContent(detail) {
  if (state.activeRunTab === "overview") {
    return kvPanel("Run", [
      ["Environment", detail.run.environment_id],
      ["Objective", `${detail.run.objective && detail.run.objective.name || "-"} ${detail.run.objective && detail.run.objective.direction || ""}`],
      ["Best trial", detail.run.best_trial_id],
      ["Best candidate", detail.run.best_candidate_id],
    ]);
  }
  if (state.activeRunTab === "trials") return tableFromRows(trialRows(detail), [["trial_id", "Trial"], ["status", "Status"], ["candidate_id", "Candidate"], ["backend", "Backend"], ["budget", "Budget"], ["error", "Error"]]);
  if (state.activeRunTab === "candidates") return tableFromRows((detail.candidates || []).map(candidateRecord), [["candidate_id", "Candidate"], ["format", "Format"], ["validation", "Validation"], ["generator", "Generator"]]);
  if (state.activeRunTab === "events") {
    const events = [
      ...(detail.method_calls || []).map((record) => ({ type: "method call", ...record })),
      ...(detail.method_events || []).map((record) => ({ type: "method event", ...record })),
      ...(detail.scheduler_events || []).map((record) => ({ type: "scheduler", ...record })),
    ];
    return tableFromRows(events, [["type", "Type"], ["event", "Event"], ["method_id", "Method"], ["created_at", "Created"], ["payload", "Payload"]]);
  }
  if (state.activeRunTab === "runtime") {
    return `<pre class="code-box">${escapeHtml(JSON.stringify({ policy: detail.run_policy, environment_snapshot: detail.environment_snapshot, lineage: detail.run_lineage }, null, 2))}</pre>`;
  }
  return filesTab(detail);
}

async function openComponentSession(component, mode) {
  try {
    const action = mode === "edit" ? "edit-copy" : "open-workspace";
    const agentSession = currentAgentSession();
    const payload = await postJson(`/api/catalog/${encodeURIComponent(component.kind)}/${encodeURIComponent(component.entry.uid)}/${action}`, { session_id: agentSession ? agentSession.id : "" });
    if (payload.workspace) {
      const session = mergeUiWorkspace(payload.workspace);
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      await attachWorkspaceToCurrent(session.id);
      setView("workspace");
      return;
    }
  } catch (error) {
    pushAssistantMessage(["tool", "Workspace open failed", String(error.message || error)]);
    setAssistantOpen(true);
    renderAssistant();
  }
}

async function launchComponentInterface(component) {
  const launchKey = componentLaunchKey(component);
  if (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) return;
  state.interfaceLaunch = {
    key: launchKey,
    label: component.entry && component.entry.interface && component.entry.interface.label,
    port: component.entry && component.entry.interface && component.entry.interface.port,
    startedAt: Date.now(),
  };
  renderComponentDetail();
  try {
    const payload = await postJson(`/api/catalog/${encodeURIComponent(component.kind)}/${encodeURIComponent(component.entry.uid)}/launch-interface-job`, {});
    const launch = payload.launch || {};
    state.interfaceLaunch = { ...state.interfaceLaunch, ...launch, key: launchKey };
    renderComponentDetail();
    await pollComponentInterfaceLaunch(launchKey, launch.launch_id);
  } catch (error) {
    if (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
      state.interfaceLaunch = null;
      renderComponentDetail();
    }
    pushAssistantMessage(["tool", "Interface launch failed", String(error.message || error)]);
    setAssistantOpen(true);
    renderAssistant();
  }
}

async function pollComponentInterfaceLaunch(launchKey, launchId) {
  if (!launchId) throw new Error("Interface launch did not return a launch id.");
  while (state.interfaceLaunch && state.interfaceLaunch.key === launchKey) {
    const payload = await getJson(`/api/interface-launches/${encodeURIComponent(launchId)}`);
    const launch = payload.launch || {};
    state.interfaceLaunch = { ...state.interfaceLaunch, ...launch, key: launchKey };
    renderComponentDetail();
    if (launch.status === "ready") {
      const result = launch.result || {};
      if (!result.workspace) throw new Error("Interface launch completed without a workspace.");
      state.interfaceLaunch = null;
      const session = mergeUiWorkspace(result.workspace);
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      await attachWorkspaceToCurrent(session.id);
      applyWorkspacePreviewPayload(session, result.preview, result.interface);
      state.workbenchMode = "preview";
      setView("workspace");
      return;
    }
    if (launch.status === "failed") {
      throw new Error(launch.error || "Interface launch failed.");
    }
    await sleep(1000);
  }
}

function applyWorkspacePreviewPayload(session, previewPayload, interfaceConfig = {}) {
  if (!session || !previewPayload || !previewPayload.preview_url) return;
  const preview = currentWorkspacePreview(session);
  preview.port = Number(previewPayload.port || interfaceConfig.port || preview.port || 5173);
  preview.url = String(previewPayload.preview_url || "");
  preview.status = "ready";
  const label = interfaceConfig && interfaceConfig.label ? interfaceConfig.label : "interface";
  preview.message = `Previewing ${label} on port ${preview.port} through ${session.title}.`;
  if (previewPayload.code_server && typeof previewPayload.code_server === "object") {
    state.codeServer = previewPayload.code_server;
    if (previewPayload.code_server.open_url) {
      state.embeddedCodeUrl = previewPayload.code_server.open_url;
      state.embeddedCodeFolder = previewPayload.folder || session.codeFolder || "";
      state.codeWorkspaceStatus = "ready";
      state.codeWorkspaceMessage = "";
    }
  }
  session.timeline.push(["tool", "interface launched", preview.message]);
}

async function createBlankSession() {
  try {
    const title = nextDraftWorkspaceTitle();
    const payload = await postJson("/api/workspaces", {
      title,
      description: "Draft project workspace",
      attached_sessions: state.selectedAgentSessionId ? [state.selectedAgentSessionId] : [],
    });
    if (payload.workspace) {
      const session = mergeUiWorkspace(payload.workspace);
      state.selectedSessionId = session.id;
      state.selectedFileKey = firstFileKey(session);
      await attachWorkspaceToCurrent(session.id);
      setView("workspace");
      return;
    }
  } catch (error) {
    pushAssistantMessage(["tool", "Workspace create failed", String(error.message || error)]);
    setAssistantOpen(true);
    renderAssistant();
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
  await savePlanDraft(plan, { render: true });
}

async function savePlanDraft(plan, options = {}) {
  if (!plan.environment || !plan.method) return;
  if (plan.study) convertSavedPlanToDraft(plan);
  const result = await postJson("/api/studies/draft", planPayload(plan), { tolerateError: true });
  plan.draft = result;
  plan.yaml = result.yaml || plan.yaml;
  plan.status = result.validation && result.validation.valid ? "ready" : "review";
  if (options.render !== false) renderExperiments();
  return result;
}

async function launchPlan(plan) {
  if (plan.study && plan.study.path) {
    const launched = await postJson("/api/studies/launch", { study_path: plan.study.path, output_root: "runs" }, { tolerateError: true });
    afterLaunch(launched);
    return;
  }
  if (!plan.draft || !plan.draft.path || (plan.draft.validation && !plan.draft.validation.valid)) {
    const saved = await savePlanDraft(plan, { render: false });
    if (!saved || saved.validation && !saved.validation.valid) {
      pushAssistantMessage(["assistant", "Config needs review", "The study config was saved but validation did not pass, so I did not launch it."]);
      renderAssistant();
      renderExperiments();
      return;
    }
  }
  const launched = await postJson("/api/studies/launch", { study_path: plan.draft.path, output_root: "runs" }, { tolerateError: true });
  afterLaunch(launched);
}

async function afterLaunch(launched) {
  if (launched.job) {
    state.pendingJobId = launched.job.job_id;
    state.selectedRunId = `job:${launched.job.job_id}`;
    state.selectedRun = { job: launched.job, run: jobRunSummary(launched.job) };
    await loadRunsAndJobs();
    setView("runs");
  } else {
    renderExperiments();
  }
}

function isEmbeddedCodeWorkspaceActive() {
  return Boolean(state.embeddedCodeUrl || els.embeddedCodeWorkspace && els.embeddedCodeWorkspace.getAttribute("src"));
}

function codeFolderForSession(session) {
  return session && (session.codeFolder || session.path);
}

function workspacePreviewKey(session = currentSession()) {
  return session ? session.backendWorkspaceId || session.id : "";
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
  return state.embeddedCodeFolder !== codeFolderForSession(session) || !state.embeddedCodeUrl;
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

async function openCodeServerEmbedded() {
  const session = currentSession();
  if (!session) return;
  const folder = codeFolderForSession(session);
  state.workbenchMode = "code";
  if (state.embeddedCodeUrl && state.embeddedCodeFolder === folder) {
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
  const result = await postJson("/api/code-server/start", { folder }, { tolerateError: true });
  state.codeServer = result;
  if (result.open_url) {
    state.embeddedCodeUrl = result.open_url;
    state.embeddedCodeFolder = folder;
    state.codeWorkspaceStatus = "ready";
    state.codeWorkspaceMessage = "";
    els.embeddedCodeWorkspace.src = result.open_url;
    session.timeline.push(["tool", "code-server embedded", `Folder: ${shortPath(result.folder || folder)}.`]);
  } else {
    state.codeWorkspaceStatus = "error";
    state.codeWorkspaceMessage = result.error || result.install_hint || "Install coder/code-server and refresh.";
    session.timeline.push(["tool", "code-server unavailable", state.codeWorkspaceMessage]);
  }
  renderWorkspace();
}

async function openCodeServerFull() {
  const session = currentSession();
  if (!session) return;
  const result = await postJson("/api/code-server/start", { folder: session.codeFolder || session.path }, { tolerateError: true });
  state.codeServer = result;
  if (result.open_url) window.open(result.open_url, "_blank", "noopener");
  renderWorkspace();
}

function reloadEmbeddedCodeWorkspace() {
  if (state.embeddedCodeUrl) {
    els.embeddedCodeWorkspace.src = state.embeddedCodeUrl;
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
  const preview = currentWorkspacePreview();
  if (preview.url) window.open(preview.url, "_blank", "noopener");
}

async function openActiveWorkspaceExternal() {
  if (state.workbenchMode === "preview") {
    openWorkspacePreviewExternal();
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

async function primaryAction() {
  if (state.view === "experiments") {
    const plan = currentPlan();
    if (plan) launchPlan(plan);
    return;
  }
  await openRegistrationMenu();
}

async function handleAgentActionButton() {
  if (assistantIsBusy()) {
    await cancelAgentMessage();
    return;
  }
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
  if (assistantIsBusy()) return;
  const message = els.agentInput.value.trim();
  if (!message) return;
  const userMessage = ["user", "User", message];
  pushAssistantMessage(userMessage);
  const session = currentAgentSession();
  if (session && !session.id.startsWith("agent-session-")) session.status = "running";
  els.agentInput.value = "";
  delete els.agentInput.dataset.touched;
  renderAssistant();
  const persisted = await persistAssistantMessage(userMessage, { keepalive: true, sessionId: session && session.id });
  if (!persisted) {
    pushAssistantMessage(["assistant", "Runtime unavailable", "This message is visible in the local transcript, but the backend assistant session could not store it."]);
  }
  renderAssistant();
}

function planPayload(plan) {
  return {
    environment_path: plan.environment.path,
    method_path: plan.method.path,
    name: `${plan.environment.id}-${plan.method.id}`,
    metric: plan.metric || firstMetric(plan.environment) || "score",
    direction: plan.direction || "maximize",
    aggregation: plan.aggregation || "mean",
    secondaryMetrics: plan.secondaryMetrics || [],
    maxTrials: Number(plan.maxTrials || 8),
    maxFailures: positiveOptionalNumber(plan.maxFailures),
    backend: plan.backend || "local",
    parallelism: Number(plan.parallelism || 1),
    timeoutSeconds: Number(plan.timeoutSeconds || 120),
    evidenceLevel: plan.evidenceLevel || "standard",
    evidenceStorage: plan.evidenceStorage || "reference",
    seed: plan.seed === "" || plan.seed === null || plan.seed === undefined ? null : Number(plan.seed),
  };
}

function upsertSession(session) {
  state.sessions = [session, ...state.sessions.filter((item) => item.id !== session.id)];
}

function upsertPlan(plan) {
  state.plans = [plan, ...state.plans.filter((item) => item.id !== plan.id)];
}

function currentSession() {
  const attached = new Set(attachedWorkspaceIds());
  return state.sessions.find((session) => session.id === state.selectedSessionId && attached.has(session.id)) || null;
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

function catalogEntryByPath(kind, path) {
  if (!path) return null;
  const entries = kind === "method" ? state.catalog.methods : state.catalog.environments;
  return (entries || []).find((entry) => entry.path === path) || null;
}

function catalogReference(kind, path) {
  if (!path) return null;
  const label = shortPath(path).split("/").pop() || kind;
  return { id: label.replace(/\.ya?ml$/, ""), label, path, summary: {} };
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
    metric,
    direction: directionForMetric(metric),
    aggregation: "mean",
    secondaryMetrics,
    maxTrials: 8,
    maxFailures: "",
    backend: "local",
    parallelism: 1,
    timeoutSeconds,
    evidenceLevel: "standard",
    evidenceStorage: "reference",
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
    `name: ${plan.environment && plan.method ? `${plan.environment.id}-${plan.method.id}` : slug(plan.title || "study")}`,
    "",
  ];
  if (plan.environment) lines.push(`environmentConfig: ${plan.environment.path}`);
  if (plan.method) lines.push(`methodConfig: ${plan.method.path}`);
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
  const maxFailures = positiveOptionalNumber(plan.maxFailures);
  if (maxFailures !== null) {
    lines.push(`  maxFailures: ${maxFailures}`);
  }
  lines.push(
    "",
    "execution:",
    `  backend: ${plan.backend || "local"}`,
    `  parallelism: ${Number(plan.parallelism || 1)}`,
  );
  if (plan.timeoutSeconds !== "" && plan.timeoutSeconds !== null && plan.timeoutSeconds !== undefined) {
    lines.push(`  timeoutSeconds: ${Number(plan.timeoutSeconds || 0)}`);
  }
  lines.push(
    "",
    "evidence:",
    `  level: ${plan.evidenceLevel || "standard"}`,
    `  outputFileStorage: ${plan.evidenceStorage || "reference"}`,
  );
  if (plan.seed !== "" && plan.seed !== null && plan.seed !== undefined) {
    lines.push("", "reproducibility:", `  seed: ${Number(plan.seed || 0)}`);
  }
  return lines.join("\n");
}

function compatibilityChecks(pair) {
  const checks = pair.checks && pair.checks.length ? pair.checks : (pair.reasons || []).map((message) => ({ ok: pair.compatible, message }));
  return checks.map((check) => ["Compatibility", check.message, check.ok ? "compatible" : "review"]);
}

function studyReadinessPanel(plan) {
  const rows = studyReadinessRows(plan);
  if (!rows.length) return "";
  return `
    <div class="readiness-panel">
      <h3>Readiness</h3>
      <div class="readiness-list">${rows.map(readinessRow).join("")}</div>
    </div>
  `;
}

function studyReadinessRows(plan) {
  const rows = [];
  if (plan.environment && plan.method) {
    rows.push(["Binding", "Environment and method references are resolved.", "ready"]);
  }
  if (plan.study && plan.study.path) {
    rows.push(["Catalog config", "Loaded from Catalog. Editing saves a separate copy.", "ready"]);
    return rows;
  }
  for (const check of plan.checks || []) rows.push(check);
  if (plan.draft && plan.draft.validation) {
    const valid = Boolean(plan.draft.validation.valid);
    rows.push(["Study config", valid ? "Schema validation passed." : "Schema validation needs review.", valid ? "valid" : "review"]);
  } else {
    rows.push(["Study config", "Save the config to run schema validation before launch.", "review"]);
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

function trialRows(detail) {
  const observationsByTrial = new Map((detail.observations || []).map((observation) => [observation.trial_id, observation]));
  return (detail.trials || []).map((trial) => {
    const observation = observationsByTrial.get(trial.trial_id) || {};
    return {
      trial_id: trial.trial_id,
      status: trial.status || observation.status,
      candidate_id: trial.candidate_id || observation.candidate_id,
      backend: backendSummary(trial.backend_worker || observation.provenance && observation.provenance.backend_worker),
      budget: budgetSummary(trial, observation),
      error: errorSummary(observation) || errorSummary(trial),
    };
  });
}

function candidateRecord(record) {
  return { ...record, candidate_id: record.candidate_id, format: record.format, generator: record.generator };
}

function filesTab(detail) {
  const files = detail.files || [];
  return `
    <div class="file-layout">
      <div class="file-list">${files.map((file) => `<button class="file-button" type="button" data-file-path="${escapeHtml(file.relative_path)}">${escapeHtml(file.relative_path)}<span>${formatBytes(file.size)}</span></button>`).join("") || emptyInline("No files found.")}</div>
      <div id="filePreview" class="file-preview"></div>
    </div>
  `;
}

function entityHeader(item, kind) {
  return `
    <div class="detail-heading">
      <div class="detail-title-block">
        <div class="detail-title-line">
          <h2>${escapeHtml(item.label)}</h2>
          <span class="catalog-kind-chip catalog-kind-${escapeHtml(kind)}">${escapeHtml(kind)}</span>
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
  const destructiveLabel = workspaceDestructiveLabel(session);
  return `
    <div class="session-card ${active ? "active" : ""} ${attached ? "attached" : "unattached"}">
      <button class="session-main" data-session-id="${escapeHtml(session.id)}" type="button">
        <strong>${escapeHtml(session.title)}</strong>
        <span>${escapeHtml(workspaceSubtitle(session))}</span>
        ${workspaceBadges(session)}
      </button>
      ${attached
        ? `<button class="workspace-close-button" data-close-workspace-id="${escapeHtml(session.id)}" type="button" title="Detach from this assistant session">Detach</button>`
        : `<button class="workspace-close-button" data-attach-workspace-id="${escapeHtml(session.id)}" type="button" title="Attach to this assistant session">Attach</button>`}
      ${(active || !attached) && canDelete ? `
        <div class="session-card-actions">
          <button class="ghost-button compact-action" data-delete-workspace-id="${escapeHtml(session.id)}" type="button">${escapeHtml(destructiveLabel)}</button>
        </div>
      ` : ""}
    </div>
  `;
}

function workspaceSubtitle(session) {
  const mode = session.mode || "editable";
  return `${mode} ${workspaceTypeLabel(session)}`;
}

function workspaceBadges(session) {
  if (session.kind === "run" || session.sourceType === "run") {
    return '<span class="workspace-badges"><span class="tag">run evidence</span></span>';
  }
  if (session.sourceType === "catalog" || session.mode === "read-only") {
    const label = session.kind && session.kind !== "catalog" ? `catalog ${session.kind}` : "catalog asset";
    return `<span class="workspace-badges"><span class="tag">${escapeHtml(label)}</span></span>`;
  }
  const entries = session.registeredEntries || [];
  if (!entries.length) {
    const label = session.managedByStudio ? "draft" : "unregistered";
    return `<span class="workspace-badges"><span class="tag">${escapeHtml(label)}</span></span>`;
  }
  return `<span class="workspace-badges">${entries.map((entry) => `<span class="tag">${escapeHtml(entry.kind)}: ${escapeHtml(entry.id)}</span>`).join("")}</span>`;
}

function workspaceTypeLabel(session) {
  if (!session) return "workspace";
  if (session.sourceType === "blank" || session.sourceType === "workspace") return "project workspace";
  if (session.kind === "workspace") return "project workspace";
  if (session.kind === "experiment plan") return "study workspace";
  if (session.kind === "run") return "run workspace";
  if (session.sourceType === "catalog-copy") return `${session.kind} copy`;
  if (session.sourceType === "catalog") return `catalog ${session.kind}`;
  return `${session.kind} workspace`;
}

function agentSessionCard(session) {
  const attachedCount = attachedWorkspaceIds(session.id).length;
  return `
    <button class="agent-session-card ${session.id === state.selectedAgentSessionId ? "active" : ""}" data-agent-session-id="${escapeHtml(session.id)}" type="button">
      <strong>${escapeHtml(session.title)}</strong>
      <span>${escapeHtml(session.description || "Conversation")}</span>
      <span class="path-text">${attachedCount} workspace${attachedCount === 1 ? "" : "s"} attached</span>
    </button>
  `;
}

function componentButton(component) {
  const item = component.entry;
  const selected = component.key === state.selectedComponentKey;
  return `
    <button class="entity-button ${selected ? "selected" : ""}" data-component-key="${escapeHtml(component.key)}" type="button">
      <span class="entity-button-header">
        <strong>${escapeHtml(item.label)}</strong>
        <span class="catalog-kind-chip catalog-kind-${escapeHtml(component.kind)}">${escapeHtml(component.kind)}</span>
      </span>
      <span class="tag-row">${(item.tags || []).slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</span>
    </button>
  `;
}

function planButton(plan) {
  const selected = plan.id === state.selectedPlanId;
  const showState = plan.status && plan.status !== "saved";
  return `
    <button class="plan-button ${selected ? "selected" : ""}" data-plan-id="${escapeHtml(plan.id)}" type="button">
      <span class="entity-button-header">
        <strong>${escapeHtml(plan.title)}</strong>
        ${showState ? statusPill(plan.status) : ""}
      </span>
      <span class="tag-row">
        ${plan.metric ? `<span class="tag">${escapeHtml(plan.metric)}</span>` : ""}
        ${plan.direction ? `<span class="tag">${escapeHtml(plan.direction)}</span>` : ""}
        ${plan.maxTrials ? `<span class="tag">${escapeHtml(plan.maxTrials)} trials</span>` : ""}
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
          <button class="ghost-button compact-action" data-build-study-index="${index}" type="button">Build Study</button>
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
  return `
    <button class="run-row ${run.id === state.selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}" type="button">
      <span class="run-row-main">
        <strong title="${escapeHtml(run.name)}">${escapeHtml(run.name)}</strong>
        <span class="path-text" title="${escapeHtml(shortPath(run.path))}">${escapeHtml(shortPath(run.path))}</span>
      </span>
      ${statusPill(run.status)}
      <span class="run-row-meta">
        <span title="${escapeHtml(run.method && run.method.id || "-")}">${escapeHtml(run.method && run.method.id || "-")}</span>
        <span>${escapeHtml(run.completed_trials ?? 0)} trials</span>
        <span>${formatMetric(run.best_metric)}</span>
      </span>
    </button>
  `;
}

function metricChart(observations, metricName) {
  const points = observations
    .map((observation, index) => ({ index, value: Number(observation.metric_values && observation.metric_values[metricName]) }))
    .filter((point) => Number.isFinite(point.value));
  if (!metricName || points.length === 0) return `<div class="chart empty-chart">No metric values to chart.</div>`;
  const width = 720;
  const height = 150;
  const pad = 20;
  const min = Math.min(...points.map((point) => point.value));
  const max = Math.max(...points.map((point) => point.value));
  const span = max - min || 1;
  const x = (point) => pad + (point.index / Math.max(1, observations.length - 1)) * (width - pad * 2);
  const y = (point) => height - pad - ((point.value - min) / span) * (height - pad * 2);
  return `
    <div class="chart">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(metricName)} over trials">
        <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" />
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" />
        <polyline points="${points.map((point) => `${x(point)},${y(point)}`).join(" ")}" />
        ${points.map((point) => `<circle cx="${x(point)}" cy="${y(point)}" r="3"><title>${metricName}: ${point.value}</title></circle>`).join("")}
        <text x="${pad}" y="14">${escapeHtml(metricName)} max ${formatMetric(max)} min ${formatMetric(min)}</text>
      </svg>
    </div>
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
  const errors = (result && result.errors || []).map((error) => `<li>${escapeHtml(error)}</li>`).join("");
  return `
    <div class="validation-header">
      ${statusPill(result && result.launched ? "launched" : valid ? "valid" : "invalid")}
      ${result && result.name ? `<strong>${escapeHtml(result.name)}</strong>` : ""}
    </div>
    ${valid || result && result.launched ? `<p class="path-text">${escapeHtml(shortPath(result.path || result.job_id || ""))}</p>` : `<ul class="error-list">${errors || "<li>Validation failed.</li>"}</ul>`}
  `;
}

function backendSummary(worker) {
  if (!worker || typeof worker !== "object") return "-";
  const backend = worker.backend || worker.worker_pool || "-";
  const details = [worker.pid ? `pid ${worker.pid}` : "", worker.timeoutSeconds ? `${worker.timeoutSeconds}s limit` : ""].filter(Boolean);
  return details.length ? `${backend} (${details.join(", ")})` : backend;
}

function budgetSummary(trial, observation) {
  const requested = trial.resource_profile || observation.resource_usage && observation.resource_usage.requested || {};
  const timeout = requested.timeoutSeconds ? `${requested.timeoutSeconds}s requested` : "";
  const elapsed = observation.resource_usage && Number.isFinite(Number(observation.resource_usage.wallClockSeconds)) ? `${Number(observation.resource_usage.wallClockSeconds).toFixed(1)}s elapsed` : "";
  return [timeout, elapsed].filter(Boolean).join(", ") || "-";
}

function errorSummary(record) {
  const summary = record && record.event_summary || {};
  const error = summary.error || summary.errors && summary.errors[0] || record && record.error;
  if (!error) return "-";
  const phase = error.phase ? `${error.phase}: ` : "";
  const kind = error.type || "Error";
  const message = error.message ? ` - ${error.message}` : "";
  return `${phase}${kind}${message}`;
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
  if (!response.ok && !options.tolerateError) throw new Error(json.error || `${response.status} ${response.statusText}`);
  return json;
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
  if (["success", "completed", "compatible", "ready", "valid", "launched", "passed", "editable", "registered", "available", "saved", "connected", "docker", "podman"].includes(value)) return "status-ready";
  if (["failed", "invalid", "incompatible", "unavailable", "offline", "missing", "off", "setup"].includes(value)) return "status-failed";
  if (["running", "validating", "opening"].includes(value)) return "status-running";
  if (["review", "draft", "read-only", "idle", "optional", "host", "chat", "limited"].includes(value)) return "status-review";
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
