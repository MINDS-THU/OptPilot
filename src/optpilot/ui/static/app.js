const state = {
  view: "home",
  workspace: null,
  runtime: null,
  catalog: { environments: [], methods: [], studies: [], builtins: {} },
  compatibility: { pairs: [] },
  runs: [],
  jobs: [],
  selectedEnvironmentUid: null,
  selectedMethodUid: null,
  selectedRunId: null,
  selectedRun: null,
  activeRunTab: "overview",
  draft: null,
  pendingJobId: null,
  configEditorPath: "",
};

const els = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  loadAll();
  setInterval(loadRunsAndJobs, 3000);
});

function cacheElements() {
  for (const id of [
    "healthStatus",
    "pageTitle",
    "pageSubtitle",
    "refreshButton",
    "homeEnvironmentCount",
    "homeMethodCount",
    "homeRunCount",
    "homeRunningCount",
    "runtimeHealth",
    "homeRecentRuns",
    "environmentFilter",
    "environmentsList",
    "environmentDetail",
    "methodFilter",
    "methodsList",
    "methodDetail",
    "builderForm",
    "builderEnvironment",
    "builderMethod",
    "builderCompatibility",
    "builderName",
    "builderMetric",
    "builderDirection",
    "builderMaxTrials",
    "builderBackend",
    "containerBackendFields",
    "builderContainerImage",
    "builderContainerExecutable",
    "builderContainerBuildContext",
    "builderContainerBuildDockerfile",
    "builderContainerBuildTag",
    "builderParallelism",
    "builderTimeout",
    "builderOutputRoot",
    "builderInstances",
    "draftButton",
    "launchDraftButton",
    "builderValidation",
    "builderYaml",
    "totalRuns",
    "runningRuns",
    "completedTrials",
    "failureCount",
    "runFilter",
    "runsTable",
    "runDetail",
    "compareRunA",
    "compareRunB",
    "compareRunsButton",
    "compareRunsResult",
    "jobsList",
    "configEditorSelect",
    "configEditorPath",
    "configOpenButton",
    "configSaveButton",
    "configEditorStatus",
    "configEditorContent",
  ]) {
    els[id] = document.getElementById(id);
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.viewTarget));
  });
  els.refreshButton.addEventListener("click", loadAll);
  els.environmentFilter.addEventListener("input", renderEnvironments);
  els.methodFilter.addEventListener("input", renderMethods);
  els.runFilter.addEventListener("input", renderRuns);
  els.builderEnvironment.addEventListener("change", () => {
    state.selectedEnvironmentUid = els.builderEnvironment.value || state.selectedEnvironmentUid;
    fillBuilderDefaults();
    renderBuilderCompatibility();
  });
  els.builderMethod.addEventListener("change", () => {
    state.selectedMethodUid = els.builderMethod.value || state.selectedMethodUid;
    fillBuilderDefaults();
    renderBuilderCompatibility();
  });
  els.draftButton.addEventListener("click", generateDraft);
  els.builderForm.addEventListener("submit", launchDraft);
  els.builderBackend.addEventListener("change", renderBackendFields);
  els.compareRunsButton.addEventListener("click", compareRuns);
  els.configEditorSelect.addEventListener("change", () => {
    const selected = configEntryByPath(els.configEditorSelect.value);
    if (!selected) return;
    els.configEditorPath.value = shortPath(selected.path);
    openConfigPath(selected.path);
  });
  els.configOpenButton.addEventListener("click", () => openConfigPath(els.configEditorPath.value));
  els.configSaveButton.addEventListener("click", saveConfigFile);
}

async function loadAll() {
  await Promise.all([loadWorkspace(), loadRuntimeHealth(), loadCatalogAndCompatibility(), loadRunsAndJobs()]);
  chooseDefaults();
  renderAll();
}

async function loadWorkspace() {
  try {
    state.workspace = await getJson("/api/workspace");
    els.healthStatus.textContent = "Ready";
  } catch (error) {
    state.workspace = null;
    els.healthStatus.textContent = "Unavailable";
  }
}

async function loadRuntimeHealth() {
  state.runtime = await getJson("/api/runtime/health");
}

async function loadCatalogAndCompatibility() {
  const [catalog, compatibility] = await Promise.all([getJson("/api/catalog"), getJson("/api/compatibility")]);
  state.catalog = catalog;
  state.compatibility = compatibility;
}

async function loadRunsAndJobs() {
  const [runsPayload, jobsPayload] = await Promise.all([getJson("/api/runs"), getJson("/api/jobs")]);
  state.runs = runsPayload.runs || [];
  state.jobs = jobsPayload.jobs || [];
  if (state.pendingJobId) {
    const job = state.jobs.find((item) => item.job_id === state.pendingJobId);
    if (job && job.run_dir) {
      const run = state.runs.find((item) => item.path === job.run_dir);
      if (run) {
        state.pendingJobId = null;
        await loadRunDetail(run.id);
        setView("runs");
      }
    }
  }
  renderHome();
  renderRuns();
  renderJobs();
}

function chooseDefaults() {
  if (!state.selectedEnvironmentUid && state.catalog.environments.length) {
    state.selectedEnvironmentUid = state.catalog.environments[0].uid;
  }
  if (!state.selectedMethodUid && state.catalog.methods.length) {
    const compatible = compatibleMethodsForEnvironment(state.selectedEnvironmentUid);
    state.selectedMethodUid = compatible[0] ? compatible[0].method.uid : state.catalog.methods[0].uid;
  }
  if (!state.selectedRunId && state.runs.length) {
    state.selectedRunId = state.runs[0].id;
  }
}

function renderAll() {
  renderHome();
  renderEnvironments();
  renderMethods();
  renderBuilder();
  renderRuns();
  renderJobs();
  renderConfigEditorOptions();
  if (state.selectedRunId) {
    loadRunDetail(state.selectedRunId, { keepTab: true });
  } else {
    renderRunDetail();
  }
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active-view", section.id === `${view}View`);
  });
  const titles = {
    home: ["Home", "Connect methods to environments, launch studies, and inspect evidence."],
    environments: ["Environments", "Inspect candidate contracts and see compatible methods."],
    methods: ["Methods", "Inspect optimization methods and their compatible environments."],
    builder: ["Study Builder", "Create a valid study from an environment and method."],
    runs: ["Runs", "Monitor jobs, compare outcomes, and inspect run evidence."],
    config: ["Config Editor", "Open and lightly edit YAML, JSON, and small support files."],
  };
  els.pageTitle.textContent = titles[view][0];
  els.pageSubtitle.textContent = titles[view][1];
}

function renderHome() {
  els.homeEnvironmentCount.textContent = String(state.catalog.environments.length);
  els.homeMethodCount.textContent = String(state.catalog.methods.length);
  els.homeRunCount.textContent = String(state.runs.length);
  els.homeRunningCount.textContent = String(state.runs.filter((run) => run.status === "running").length);
  renderRuntimeHealth();
  els.homeRecentRuns.innerHTML = state.runs.slice(0, 6).map(renderRunCard).join("") || emptyInline("No runs discovered yet.");
  document.querySelectorAll(".home-run-card").forEach((button) => {
    button.addEventListener("click", async () => {
      await loadRunDetail(button.dataset.runId);
      setView("runs");
    });
  });
}

function renderRuntimeHealth() {
  if (!state.runtime) {
    els.runtimeHealth.innerHTML = emptyState("Runtime health is unavailable.");
    return;
  }
  const items = [
    ["Python", state.runtime.python],
    ["Docker", state.runtime.docker],
    ["Podman", state.runtime.podman],
  ];
  els.runtimeHealth.innerHTML = items
    .map(([label, info]) => `
      <div class="list-item compact">
        <div class="row-between">
          <strong>${escapeHtml(label)}</strong>
          ${statusPill(info && info.ok ? "ready" : "unavailable")}
        </div>
        <p class="path-text">${escapeHtml((info && (info.version || info.path)) || "Not found")}</p>
      </div>
    `)
    .join("");
}

function renderEnvironments() {
  const query = els.environmentFilter.value.trim().toLowerCase();
  const environments = state.catalog.environments.filter((item) => searchable(item).includes(query));
  els.environmentsList.innerHTML = environments.map((item) => entityButton(item, "environment")).join("") || emptyInline("No environments match.");
  document.querySelectorAll("[data-environment-uid]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedEnvironmentUid = button.dataset.environmentUid;
      renderEnvironments();
      fillBuilderFromSelection();
    });
  });
  renderEnvironmentDetail();
}

function renderEnvironmentDetail() {
  const env = environmentByUid(state.selectedEnvironmentUid);
  if (!env) {
    els.environmentDetail.innerHTML = emptyState("Select an environment to inspect its contract.");
    return;
  }
  const compatible = compatibleMethodsForEnvironment(env.uid);
  const incompatible = incompatibleMethodsForEnvironment(env.uid);
  els.environmentDetail.innerHTML = `
    ${entityHeader(env)}
    <div class="detail-grid">
      ${kvPanel("Candidate Contract", [
        ["Format", env.summary.candidate_format],
        ["Metrics", (env.summary.metrics || []).join(", ") || "-"],
        ["Evaluator", env.summary.evaluate_type],
      ])}
      ${kvPanel("Runtime", [
        ["Evaluator type", env.summary.runtime && env.summary.runtime.evaluate_type],
        ["Timeout", env.summary.runtime && env.summary.runtime.timeoutSeconds],
        ["Python path", env.summary.runtime && env.summary.runtime.has_python_path ? "configured" : "default"],
      ])}
    </div>
    <div class="panel-section">
      <div class="section-heading">
        <h3>Compatible Methods</h3>
        <button class="ghost-button create-from-env" type="button">Create study</button>
      </div>
      ${compatList(compatible, "method")}
    </div>
    <div class="panel-section">
      <h3>Incompatible Methods</h3>
      ${compatList(incompatible, "method")}
    </div>
  `;
  const create = document.querySelector(".create-from-env");
  if (create) {
    create.addEventListener("click", () => {
      const compatibleMethod = compatible[0];
      if (compatibleMethod) state.selectedMethodUid = compatibleMethod.method.uid;
      state.selectedEnvironmentUid = env.uid;
      setView("builder");
      renderBuilder();
    });
  }
  bindConfigEditButtons();
}

function renderMethods() {
  const query = els.methodFilter.value.trim().toLowerCase();
  const methods = state.catalog.methods.filter((item) => searchable(item).includes(query));
  els.methodsList.innerHTML = methods.map((item) => entityButton(item, "method")).join("") || emptyInline("No methods match.");
  document.querySelectorAll("[data-method-uid]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedMethodUid = button.dataset.methodUid;
      renderMethods();
      fillBuilderFromSelection();
    });
  });
  renderMethodDetail();
}

function renderMethodDetail() {
  const method = methodByUid(state.selectedMethodUid);
  if (!method) {
    els.methodDetail.innerHTML = emptyState("Select a method to inspect compatibility.");
    return;
  }
  const compatible = compatibleEnvironmentsForMethod(method.uid);
  const incompatible = incompatibleEnvironmentsForMethod(method.uid);
  els.methodDetail.innerHTML = `
    ${entityHeader(method)}
    <div class="detail-grid">
      ${kvPanel("Implementation", [
        ["Type", method.summary.implementation_type],
        ["Protocol", method.summary.protocol],
        ["Batch size", method.summary.batch_size],
      ])}
      ${kvPanel("Runtime", [
        ["Runtime", method.summary.runtime && method.summary.runtime.type],
        ["Image", method.summary.runtime && method.summary.runtime.image],
        ["Build", method.summary.runtime && method.summary.runtime.has_build ? "configured" : "not configured"],
        ["Network", method.summary.runtime && method.summary.runtime.networkPolicy],
      ])}
      ${kvPanel("Compatibility", [
        ["Candidate formats", (method.summary.candidate_formats || []).join(", ")],
        ["Capabilities", (method.summary.required_capabilities || []).join(", ") || "-"],
      ])}
    </div>
    <div class="panel-section">
      <div class="section-heading">
        <h3>Compatible Environments</h3>
        <button class="ghost-button create-from-method" type="button">Create study</button>
      </div>
      ${compatList(compatible, "environment")}
    </div>
    <div class="panel-section">
      <h3>Incompatible Environments</h3>
      ${compatList(incompatible, "environment")}
    </div>
  `;
  const create = document.querySelector(".create-from-method");
  if (create) {
    create.addEventListener("click", () => {
      const compatibleEnvironment = compatible[0];
      if (compatibleEnvironment) state.selectedEnvironmentUid = compatibleEnvironment.environment.uid;
      state.selectedMethodUid = method.uid;
      setView("builder");
      renderBuilder();
    });
  }
  bindConfigEditButtons();
}

function renderBuilder() {
  renderBuilderOptions();
  fillBuilderDefaults();
  renderBuilderCompatibility();
  renderBackendFields();
}

function renderBuilderOptions() {
  els.builderEnvironment.innerHTML = state.catalog.environments.map((env) => option(env, state.selectedEnvironmentUid)).join("");
  const compatible = new Set(compatibleMethodsForEnvironment(state.selectedEnvironmentUid).map((pair) => pair.method.uid));
  els.builderMethod.innerHTML = state.catalog.methods
    .map((method) => {
      const label = compatible.has(method.uid) ? method.label : `${method.label} (incompatible)`;
      return `<option value="${escapeHtml(method.uid)}" ${method.uid === state.selectedMethodUid ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function fillBuilderDefaults() {
  const env = environmentByUid(els.builderEnvironment.value || state.selectedEnvironmentUid);
  const method = methodByUid(els.builderMethod.value || state.selectedMethodUid);
  if (env) state.selectedEnvironmentUid = env.uid;
  if (method) state.selectedMethodUid = method.uid;
  if (!els.builderName.value && env && method) {
    els.builderName.value = `${env.id}-${method.id}`;
  }
  if (env && (!els.builderMetric.value || !((env.summary.metrics || []).includes(els.builderMetric.value)))) {
    els.builderMetric.value = (env.summary.metrics || [])[0] || "score";
  }
}

function fillBuilderFromSelection() {
  els.builderName.value = "";
  els.builderMetric.value = "";
  renderBuilder();
}

function renderBuilderCompatibility() {
  const pair = pairFor(state.selectedEnvironmentUid, state.selectedMethodUid);
  if (!pair) {
    els.builderCompatibility.innerHTML = `<div class="compat-warning">Select an environment and method.</div>`;
    return;
  }
  els.builderCompatibility.innerHTML = compatibilityBox(pair);
}

function renderBackendFields() {
  const backend = els.builderBackend.value;
  els.containerBackendFields.classList.toggle("active", backend === "container");
}

async function generateDraft() {
  const result = await postJson("/api/studies/draft", builderPayload(), { tolerateError: true });
  state.draft = result;
  renderDraft(result);
  return result;
}

async function launchDraft(event) {
  event.preventDefault();
  const result = state.draft && state.draft.path ? state.draft : await generateDraft();
  if (!result.path || (result.validation && !result.validation.valid)) {
    renderDraft(result);
    return;
  }
  const launched = await postJson("/api/studies/launch", {
    study_path: result.path,
    output_root: els.builderOutputRoot.value.trim() || "runs",
  }, { tolerateError: true });
  if (launched.job) {
    state.pendingJobId = launched.job.job_id;
    els.builderValidation.innerHTML = validationHtml({
      valid: true,
      launched: true,
      name: launched.job.study_name,
      path: launched.job.study_path,
      environment_id: launched.job.environment_id,
      job_id: launched.job.job_id,
    });
    await loadRunsAndJobs();
    setView("runs");
  } else {
    els.builderValidation.innerHTML = validationHtml(launched);
  }
}

function builderPayload() {
  return {
    environment_path: environmentByUid(els.builderEnvironment.value).path,
    method_path: methodByUid(els.builderMethod.value).path,
    name: els.builderName.value.trim(),
    metric: els.builderMetric.value.trim(),
    direction: els.builderDirection.value,
    maxTrials: Number(els.builderMaxTrials.value || 1),
    backend: els.builderBackend.value,
    containerImage: els.builderContainerImage.value.trim(),
    containerExecutable: els.builderContainerExecutable.value.trim(),
    containerBuildContext: els.builderContainerBuildContext.value.trim(),
    containerBuildDockerfile: els.builderContainerBuildDockerfile.value.trim(),
    containerBuildTag: els.builderContainerBuildTag.value.trim(),
    parallelism: Number(els.builderParallelism.value || 1),
    timeoutSeconds: Number(els.builderTimeout.value || 120),
    instances: els.builderInstances.value,
  };
}

function renderDraft(result) {
  els.builderYaml.textContent = result.yaml || "";
  els.builderValidation.innerHTML = validationHtml(result.validation || result);
  if (result.compatibility) {
    els.builderCompatibility.innerHTML = compatibilityBox(result.compatibility);
  }
}

function renderRuns() {
  const query = els.runFilter.value.trim().toLowerCase();
  const runs = state.runs.filter((run) => `${run.name} ${run.path} ${run.status} ${run.environment_id || ""} ${run.method && run.method.id || ""}`.toLowerCase().includes(query));
  els.totalRuns.textContent = String(state.runs.length);
  els.runningRuns.textContent = String(state.runs.filter((run) => run.status === "running").length);
  els.completedTrials.textContent = String(sum(state.runs.map((run) => Number(run.completed_trials || 0))));
  els.failureCount.textContent = String(sum(state.runs.map((run) => Number(run.failure_count || 0))));
  els.runsTable.innerHTML = runs.map(runRow).join("") || `<tr><td colspan="6">${emptyInline("No runs match.")}</td></tr>`;
  document.querySelectorAll(".run-row").forEach((row) => {
    row.addEventListener("click", () => loadRunDetail(row.dataset.runId));
  });
  renderCompareOptions();
}

function renderCompareOptions() {
  const previousA = els.compareRunA.value;
  const previousB = els.compareRunB.value;
  const options = state.runs.map((run) => `<option value="${escapeHtml(run.id)}">${escapeHtml(run.name)}</option>`).join("");
  els.compareRunA.innerHTML = options;
  els.compareRunB.innerHTML = options;
  if (previousA && state.runs.some((run) => run.id === previousA)) els.compareRunA.value = previousA;
  else if (state.runs[0]) els.compareRunA.value = state.runs[0].id;
  if (previousB && state.runs.some((run) => run.id === previousB)) els.compareRunB.value = previousB;
  else if (state.runs[1]) els.compareRunB.value = state.runs[1].id;
}

async function compareRuns() {
  const firstId = els.compareRunA.value;
  const secondId = els.compareRunB.value;
  if (!firstId || !secondId) {
    els.compareRunsResult.innerHTML = emptyInline("Select two runs to compare.");
    return;
  }
  const [first, second] = await Promise.all([
    getJson(`/api/runs/${encodeURIComponent(firstId)}`),
    getJson(`/api/runs/${encodeURIComponent(secondId)}`),
  ]);
  els.compareRunsResult.innerHTML = runComparisonTable(first.run, second.run);
}

async function loadRunDetail(runId, options = {}) {
  state.selectedRunId = runId;
  state.selectedRun = await getJson(`/api/runs/${encodeURIComponent(runId)}`);
  if (!options.keepTab) state.activeRunTab = "overview";
  renderRuns();
  renderRunDetail();
}

function renderRunDetail() {
  const detail = state.selectedRun;
  if (!detail) {
    els.runDetail.innerHTML = emptyState("Select a run to inspect observations, candidates, events, and files.");
    return;
  }
  const run = detail.run;
  els.runDetail.innerHTML = `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(run.name)}</h2>
        <p class="path-text">${escapeHtml(shortPath(run.path))}</p>
      </div>
      ${statusPill(run.status)}
    </div>
    <div class="detail-stats">
      <div><span>Best metric</span><strong>${formatMetric(run.best_metric)}</strong></div>
      <div><span>Trials</span><strong>${escapeHtml(run.completed_trials || 0)}</strong></div>
      <div><span>Failures</span><strong>${escapeHtml(run.failure_count || 0)}</strong></div>
      <div><span>Method</span><strong>${escapeHtml(run.method && run.method.id || "-")}</strong></div>
    </div>
    ${metricChart(detail.observations || [], run.objective && run.objective.name)}
    <div class="tabs">
      ${["overview", "trials", "candidates", "events", "runtime", "files"].map((tab) => `<button class="tab ${state.activeRunTab === tab ? "active" : ""}" data-run-tab="${tab}" type="button">${tabLabel(tab)}</button>`).join("")}
    </div>
    <div class="tab-content">${runTabContent(detail)}</div>
  `;
  document.querySelectorAll("[data-run-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeRunTab = button.dataset.runTab;
      renderRunDetail();
    });
  });
  document.querySelectorAll(".file-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await getJson(`/api/runs/${encodeURIComponent(detail.run.id)}/file?path=${encodeURIComponent(button.dataset.filePath)}`);
      const preview = document.getElementById("filePreview");
      preview.innerHTML = `<h3>${escapeHtml(response.relative_path)}</h3><pre class="code-box">${escapeHtml(response.content)}</pre>`;
    });
  });
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
  if (state.activeRunTab === "trials") {
    return tableFromRows(trialRows(detail), [
      ["trial_id", "Trial"],
      ["status", "Status"],
      ["candidate_id", "Candidate"],
      ["backend", "Backend"],
      ["budget", "Budget"],
      ["error", "Error"],
    ]);
  }
  if (state.activeRunTab === "candidates") {
    return tableFromRows((detail.candidates || []).map(candidateRecord), [["candidate_id", "Candidate"], ["format", "Format"], ["validation", "Validation"], ["generator", "Generator"]]);
  }
  if (state.activeRunTab === "events") {
    const events = [
      ...(detail.method_calls || []).map((record) => ({ type: "method call", ...record })),
      ...(detail.method_events || []).map((record) => ({ type: "method event", ...record })),
      ...(detail.scheduler_events || []).map((record) => ({ type: "scheduler", ...record })),
    ];
    return tableFromRows(events, [["type", "Type"], ["event", "Event"], ["method_id", "Method"], ["created_at", "Created"], ["payload", "Payload"]]);
  }
  if (state.activeRunTab === "runtime") {
    return `<pre class="code-box">${escapeHtml(JSON.stringify({
      policy: detail.run_policy,
      environment_snapshot: detail.environment_snapshot,
      lineage: detail.run_lineage,
    }, null, 2))}</pre>`;
  }
  return filesTab(detail);
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
  return {
    ...record,
    candidate_id: record.candidate_id,
    format: record.format,
    generator: record.generator,
  };
}

function backendSummary(worker) {
  if (!worker || typeof worker !== "object") return "-";
  const backend = worker.backend || worker.worker_pool || "-";
  const details = [
    worker.worker_pool && worker.worker_pool !== backend ? worker.worker_pool : "",
    worker.pid ? `pid ${worker.pid}` : "",
    worker.timeoutSeconds ? `${worker.timeoutSeconds}s limit` : "",
  ].filter(Boolean);
  return details.length ? `${backend} (${details.join(", ")})` : backend;
}

function budgetSummary(trial, observation) {
  const requested = trial.resource_profile || observation.resource_usage && observation.resource_usage.requested || {};
  const timeout = requested.timeoutSeconds ? `${requested.timeoutSeconds}s requested` : "";
  const elapsed = observation.resource_usage && Number.isFinite(Number(observation.resource_usage.wallClockSeconds))
    ? `${Number(observation.resource_usage.wallClockSeconds).toFixed(1)}s elapsed`
    : "";
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

function filesTab(detail) {
  const files = detail.files || [];
  return `
    <div class="file-layout">
      <div class="file-list">
        ${files.map((file) => `<button class="file-button" type="button" data-file-path="${escapeHtml(file.relative_path)}">${escapeHtml(file.relative_path)}<span>${formatBytes(file.size)}</span></button>`).join("") || emptyInline("No files found.")}
      </div>
      <div id="filePreview" class="file-preview"></div>
    </div>
  `;
}

function renderJobs() {
  els.jobsList.innerHTML = state.jobs.map((job) => `
    <div class="compact-card">
      <div class="row-between">
        <strong>${escapeHtml(job.study_name || job.job_id)}</strong>
        ${statusPill(job.status)}
      </div>
      <p class="path-text">${escapeHtml(shortPath(job.study_path))}</p>
      <div class="item-meta">
        <span class="tag">pid ${escapeHtml(job.process_id || "-")}</span>
        <span class="tag">exit ${escapeHtml(job.exit_code ?? "-")}</span>
        ${job.run_dir ? `<span class="tag">${escapeHtml(shortPath(job.run_dir))}</span>` : ""}
      </div>
      ${job.status === "running" ? `<button class="ghost-button stop-job" data-job-id="${escapeHtml(job.job_id)}" type="button">Stop</button>` : ""}
    </div>
  `).join("") || emptyInline("No UI-launched jobs yet.");
  document.querySelectorAll(".stop-job").forEach((button) => {
    button.addEventListener("click", async () => {
      await postJson(`/api/jobs/${encodeURIComponent(button.dataset.jobId)}/stop`, {});
      await loadRunsAndJobs();
    });
  });
}

function entityButton(item, kind) {
  const selected = kind === "environment" ? item.uid === state.selectedEnvironmentUid : item.uid === state.selectedMethodUid;
  const summary = item.summary || {};
  const tags = [
    summary.candidate_format,
    summary.implementation_type,
    summary.runtime && summary.runtime.type,
  ].filter(Boolean);
  return `
    <button class="entity-button ${selected ? "selected" : ""}" data-${kind}-uid="${escapeHtml(item.uid)}" type="button">
      <strong>${escapeHtml(item.label)}</strong>
      <span class="path-text">${escapeHtml(shortPath(item.path))}</span>
      <span class="tag-row">${tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</span>
    </button>
  `;
}

function entityHeader(item) {
  return `
    <div class="detail-heading">
      <div>
        <h2>${escapeHtml(item.label)}</h2>
        <p class="path-text">${escapeHtml(shortPath(item.path))}</p>
      </div>
      <div class="detail-actions">
        <div class="item-meta">${(item.tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
        <button class="ghost-button edit-config-button" data-config-path="${escapeHtml(item.path)}" type="button">Edit config</button>
      </div>
    </div>
    ${item.description ? `<p class="detail-description">${escapeHtml(item.description)}</p>` : ""}
  `;
}

function bindConfigEditButtons() {
  document.querySelectorAll(".edit-config-button").forEach((button) => {
    button.addEventListener("click", async () => {
      await openConfigPath(button.dataset.configPath);
      setView("config");
    });
  });
}

function compatList(pairs, target) {
  if (!pairs.length) return emptyInline("No entries.");
  return `<div class="compat-list">${pairs.map((pair) => {
    const item = target === "method" ? pair.method : pair.environment;
    return `
      <div class="compat-item ${pair.compatible ? "compatible" : "incompatible"}">
        <div class="row-between">
          <strong>${escapeHtml(item.label)}</strong>
          ${statusPill(pair.compatible ? "compatible" : "incompatible")}
        </div>
        <p class="path-text">${escapeHtml(shortPath(item.path))}</p>
        <ul>${pair.reasons.slice(0, 4).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
      </div>
    `;
  }).join("")}</div>`;
}

function compatibilityBox(pair) {
  return `
    <div class="${pair.compatible ? "compat-ok" : "compat-warning"}">
      <strong>${pair.compatible ? "Compatible" : "Not compatible"}</strong>
      <ul>${(pair.checks || []).map((check) => `<li>${check.ok ? "OK" : "Issue"}: ${escapeHtml(check.message)}</li>`).join("")}</ul>
    </div>
  `;
}

function kvPanel(title, rows) {
  return `
    <section class="kv-panel">
      <h3>${escapeHtml(title)}</h3>
      <dl>
        ${rows.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value ?? "-")}</dd></div>`).join("")}
      </dl>
    </section>
  `;
}

function runRow(run) {
  return `
    <tr class="run-row ${run.id === state.selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}">
      <td><strong>${escapeHtml(run.name)}</strong><div class="path-text">${escapeHtml(shortPath(run.path))}</div></td>
      <td>${statusPill(run.status)}</td>
      <td>${escapeHtml(run.method && run.method.id || "-")}</td>
      <td>${escapeHtml(run.completed_trials ?? 0)}</td>
      <td>${formatMetric(run.best_metric)}</td>
      <td>${escapeHtml(run.environment_id || "-")}</td>
    </tr>
  `;
}

function renderRunCard(run) {
  return `
    <button class="compact-card home-run-card" data-run-id="${escapeHtml(run.id)}" type="button">
      <div class="row-between">
        <strong>${escapeHtml(run.name)}</strong>
        ${statusPill(run.status)}
      </div>
      <p>${escapeHtml(run.environment_id || "-")} / ${escapeHtml(run.method && run.method.id || "-")}</p>
      <div class="item-meta">
        <span class="tag">best ${escapeHtml(formatMetric(run.best_metric))}</span>
        <span class="tag">${escapeHtml(run.completed_trials || 0)} trials</span>
      </div>
    </button>
  `;
}

function metricChart(observations, metricName) {
  const points = observations
    .map((observation, index) => ({ index, value: Number(observation.metric_values && observation.metric_values[metricName]) }))
    .filter((point) => Number.isFinite(point.value));
  if (!metricName || points.length === 0) {
    return `<div class="chart empty-chart">No metric values to chart.</div>`;
  }
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

function runComparisonTable(first, second) {
  const rows = [
    ["Name", first.name, second.name],
    ["Status", first.status, second.status],
    ["Best metric", formatMetric(first.best_metric), formatMetric(second.best_metric)],
    ["Trials", first.completed_trials ?? 0, second.completed_trials ?? 0],
    ["Failures", first.failure_count ?? 0, second.failure_count ?? 0],
    ["Method", first.method && first.method.id || "-", second.method && second.method.id || "-"],
    ["Environment", first.environment_id || "-", second.environment_id || "-"],
  ];
  return `
    <div class="table-wrap embedded">
      <table>
        <thead><tr><th>Field</th><th>${escapeHtml(first.name)}</th><th>${escapeHtml(second.name)}</th></tr></thead>
        <tbody>${rows.map(([label, a, b]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(a)}</td><td>${escapeHtml(b)}</td></tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

async function openConfigPath(path) {
  const cleanPath = String(path || "").trim();
  if (!cleanPath) {
    els.configEditorStatus.innerHTML = validationHtml({ valid: false, errors: ["Enter a file path."] });
    return;
  }
  try {
    const response = await getJson(`/api/config/file?path=${encodeURIComponent(cleanPath)}`);
    state.configEditorPath = response.path;
    els.configEditorPath.value = response.relative_path || response.path;
    syncConfigEditorSelect(response.path);
    els.configEditorContent.value = response.content || "";
    els.configEditorStatus.innerHTML = validationHtml(response.validation || { valid: true, path: response.path });
  } catch (error) {
    els.configEditorStatus.innerHTML = validationHtml({ valid: false, errors: [String(error.message || error)] });
  }
}

async function saveConfigFile() {
  const response = await postJson("/api/config/file", {
    path: els.configEditorPath.value,
    content: els.configEditorContent.value,
  }, { tolerateError: true });
  const result = response.validation || response;
  els.configEditorStatus.innerHTML = validationHtml({
    valid: Boolean(response.saved && result.valid),
    errors: result.errors || [response.error || "Save failed."],
    path: response.relative_path || response.path,
  });
  if (response.saved) {
    await loadCatalogAndCompatibility();
    renderAll();
    syncConfigEditorSelect(response.path);
  }
}

function renderConfigEditorOptions() {
  const groups = [
    ["Studies", state.catalog.studies || []],
    ["Environments", state.catalog.environments || []],
    ["Methods", state.catalog.methods || []],
  ];
  const selectedPath = state.configEditorPath || "";
  els.configEditorSelect.innerHTML = [
    `<option value="">Choose from catalog</option>`,
    ...groups
      .filter(([, items]) => items.length)
      .map(([label, items]) => `
        <optgroup label="${escapeHtml(label)}">
          ${items
            .slice()
            .sort((a, b) => String(a.label).localeCompare(String(b.label)))
            .map((item) => `<option value="${escapeHtml(item.path)}" ${item.path === selectedPath ? "selected" : ""}>${escapeHtml(item.label)} - ${escapeHtml(shortPath(item.path))}</option>`)
            .join("")}
        </optgroup>
      `),
  ].join("");
}

function syncConfigEditorSelect(path) {
  const match = configEntryByPath(path);
  els.configEditorSelect.value = match ? match.path : "";
}

function configEntryByPath(path) {
  const normalized = String(path || "");
  return [
    ...(state.catalog.studies || []),
    ...(state.catalog.environments || []),
    ...(state.catalog.methods || []),
  ].find((item) => item.path === normalized) || null;
}

function compatibleMethodsForEnvironment(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.environment.uid === uid && pair.compatible);
}

function incompatibleMethodsForEnvironment(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.environment.uid === uid && !pair.compatible);
}

function compatibleEnvironmentsForMethod(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.method.uid === uid && pair.compatible);
}

function incompatibleEnvironmentsForMethod(uid) {
  return (state.compatibility.pairs || []).filter((pair) => pair.method.uid === uid && !pair.compatible);
}

function pairFor(environmentUid, methodUid) {
  return (state.compatibility.pairs || []).find((pair) => pair.environment.uid === environmentUid && pair.method.uid === methodUid);
}

function environmentByUid(uid) {
  return (state.catalog.environments || []).find((item) => item.uid === uid) || null;
}

function methodByUid(uid) {
  return (state.catalog.methods || []).find((item) => item.uid === uid) || null;
}

function option(item, selectedUid) {
  return `<option value="${escapeHtml(item.uid)}" ${item.uid === selectedUid ? "selected" : ""}>${escapeHtml(item.label)}</option>`;
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function postJson(url, payload, options = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = await response.json();
  if (!response.ok && !options.tolerateError) {
    throw new Error(json.error || `${response.status} ${response.statusText}`);
  }
  return json;
}

function statusPill(status) {
  return `<span class="status-pill status-${escapeHtml(status || "unknown")}">${escapeHtml(status || "unknown")}</span>`;
}

function formatCell(value) {
  if (value == null || value === "") return "-";
  if (typeof value === "object") return `<pre class="path-text">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
  if (String(value).length > 80) return `<span class="path-text">${escapeHtml(String(value))}</span>`;
  return escapeHtml(String(value));
}

function formatMetric(value) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(4).replace(/\.?0+$/, "");
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
  if (cwd && String(path).startsWith(`${cwd}/`)) return String(path).slice(cwd.length + 1);
  const parts = String(path).split("/").filter(Boolean);
  if (parts.length <= 4) return String(path);
  return `.../${parts.slice(-4).join("/")}`;
}

function searchable(item) {
  return `${item.label} ${item.id} ${item.path} ${JSON.stringify(item.summary || {})}`.toLowerCase();
}

function emptyState(message) {
  return `<div class="empty-state"><p>${escapeHtml(message)}</p></div>`;
}

function emptyInline(message) {
  return `<div class="empty-inline">${escapeHtml(message)}</div>`;
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0);
}

function capitalize(value) {
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

function tabLabel(value) {
  if (value === "candidates") return "Candidates";
  return capitalize(value);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
