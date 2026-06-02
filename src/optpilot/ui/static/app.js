const state = {
  view: "studies",
  runs: [],
  catalog: null,
  jobs: [],
  selectedRunId: null,
  selectedRun: null,
  activeDetailTab: "observations",
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
    "totalRuns",
    "runningRuns",
    "completedTrials",
    "failureCount",
    "runFilter",
    "runsTable",
    "runDetailEmpty",
    "runDetail",
    "detailName",
    "detailPath",
    "detailStatus",
    "detailBest",
    "detailTrials",
    "detailFailures",
    "detailObjective",
    "metricChart",
    "detailTabContent",
    "environmentsList",
    "methodsList",
    "studiesList",
    "builtinsList",
    "launchForm",
    "studyPathInput",
    "outputRootInput",
    "validateButton",
    "validationResult",
    "jobsList",
  ]) {
    els[id] = document.getElementById(id);
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-button").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeDetailTab = button.dataset.tab;
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      renderDetailTab();
    });
  });
  els.refreshButton.addEventListener("click", loadAll);
  els.runFilter.addEventListener("input", renderRuns);
  els.validateButton.addEventListener("click", validateStudy);
  els.launchForm.addEventListener("submit", launchStudy);
}

async function loadAll() {
  await Promise.all([loadHealth(), loadCatalog(), loadRunsAndJobs()]);
}

async function loadHealth() {
  try {
    const payload = await getJson("/api/health");
    els.healthStatus.textContent = payload.ok ? "Ready" : "Unavailable";
  } catch (error) {
    els.healthStatus.textContent = "Unavailable";
  }
}

async function loadCatalog() {
  state.catalog = await getJson("/api/catalog");
  renderCatalog();
}

async function loadRunsAndJobs() {
  const [runsPayload, jobsPayload] = await Promise.all([getJson("/api/runs"), getJson("/api/jobs")]);
  state.runs = runsPayload.runs || [];
  state.jobs = jobsPayload.jobs || [];
  renderRuns();
  renderJobs();
  if (state.selectedRunId && state.runs.some((run) => run.id === state.selectedRunId)) {
    await loadRunDetail(state.selectedRunId, { keepTab: true });
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
    studies: ["Studies", "Monitor running studies and inspect previous runs."],
    catalog: ["Catalog", "Browse available environments, methods, studies, and built-ins."],
    builder: ["Builder", "Validate and launch existing StudyConfig files."],
  };
  els.pageTitle.textContent = titles[view][0];
  els.pageSubtitle.textContent = titles[view][1];
}

function renderRuns() {
  const query = els.runFilter.value.trim().toLowerCase();
  const runs = state.runs.filter((run) => {
    const haystack = `${run.name} ${run.path} ${run.status} ${run.target_id || ""}`.toLowerCase();
    return haystack.includes(query);
  });
  els.totalRuns.textContent = String(state.runs.length);
  els.runningRuns.textContent = String(state.runs.filter((run) => run.status === "running").length);
  els.completedTrials.textContent = String(sum(state.runs.map((run) => Number(run.completed_trials || 0))));
  els.failureCount.textContent = String(sum(state.runs.map((run) => Number(run.failure_count || 0))));
  els.runsTable.innerHTML = runs
    .map(
      (run) => `
        <tr class="run-row ${run.id === state.selectedRunId ? "selected" : ""}" data-run-id="${escapeHtml(run.id)}">
          <td><strong>${escapeHtml(run.name)}</strong><div class="path-text">${escapeHtml(run.path)}</div></td>
          <td>${statusPill(run.status)}</td>
          <td>${escapeHtml(run.completed_trials ?? 0)}</td>
          <td>${formatMetric(run.best_metric)}</td>
          <td>${escapeHtml(run.target_id || "-")}</td>
          <td>${formatTime(run.finished_at || run.started_at || run.updated_at)}</td>
        </tr>
      `
    )
    .join("");
  document.querySelectorAll(".run-row").forEach((row) => {
    row.addEventListener("click", () => loadRunDetail(row.dataset.runId));
  });
}

async function loadRunDetail(runId, options = {}) {
  state.selectedRunId = runId;
  state.selectedRun = await getJson(`/api/runs/${encodeURIComponent(runId)}`);
  if (!options.keepTab) {
    state.activeDetailTab = "observations";
    document.querySelectorAll(".tab").forEach((button) => {
      button.classList.toggle("active", button.dataset.tab === "observations");
    });
  }
  renderRuns();
  renderRunDetail();
}

function renderRunDetail() {
  const detail = state.selectedRun;
  if (!detail) {
    els.runDetailEmpty.classList.remove("hidden");
    els.runDetail.classList.add("hidden");
    return;
  }
  const run = detail.run;
  const objective = run.objective || {};
  els.runDetailEmpty.classList.add("hidden");
  els.runDetail.classList.remove("hidden");
  els.detailName.textContent = run.name;
  els.detailPath.textContent = run.path;
  els.detailStatus.innerHTML = statusText(run.status);
  setStatusClass(els.detailStatus, run.status);
  els.detailBest.textContent = formatMetric(run.best_metric);
  els.detailTrials.textContent = String(run.completed_trials || 0);
  els.detailFailures.textContent = String(run.failure_count || 0);
  els.detailObjective.textContent = objective.name ? `${objective.name} ${objective.direction || ""}` : "-";
  renderMetricChart(detail.observations || [], objective.name);
  renderDetailTab();
}

function renderMetricChart(observations, metricName) {
  const points = observations
    .map((observation, index) => ({
      index,
      value: Number(observation.metric_values && observation.metric_values[metricName]),
    }))
    .filter((point) => Number.isFinite(point.value));
  if (!metricName || points.length === 0) {
    els.metricChart.innerHTML = `<div class="empty-state"><p>No metric values to chart.</p></div>`;
    return;
  }
  const width = 640;
  const height = 140;
  const pad = 18;
  const min = Math.min(...points.map((point) => point.value));
  const max = Math.max(...points.map((point) => point.value));
  const span = max - min || 1;
  const x = (point) => pad + (point.index / Math.max(1, observations.length - 1)) * (width - pad * 2);
  const y = (point) => height - pad - ((point.value - min) / span) * (height - pad * 2);
  const polyline = points.map((point) => `${x(point)},${y(point)}`).join(" ");
  const circles = points
    .map((point) => `<circle cx="${x(point)}" cy="${y(point)}" r="3"><title>${metricName}: ${point.value}</title></circle>`)
    .join("");
  els.metricChart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(metricName)} over trials">
      <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#d9e1e5" />
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" stroke="#d9e1e5" />
      <polyline points="${polyline}" fill="none" stroke="#087f8c" stroke-width="2.5" />
      <g fill="#2d6cdf">${circles}</g>
      <text x="${pad}" y="13" font-size="11" fill="#66747c">${escapeHtml(metricName)} max ${formatMetric(max)} min ${formatMetric(min)}</text>
    </svg>
  `;
}

function renderDetailTab() {
  const detail = state.selectedRun;
  if (!detail) return;
  if (state.activeDetailTab === "observations") {
    els.detailTabContent.innerHTML = tableFromRows(detail.observations || [], [
      ["trial_id", "Trial"],
      ["status", "Status"],
      ["metric_values", "Metrics"],
      ["resource_usage", "Resources"],
    ]);
    return;
  }
  if (state.activeDetailTab === "artifacts") {
    els.detailTabContent.innerHTML = tableFromRows(detail.artifacts || [], [
      ["artifact_id", "Artifact"],
      ["artifact_kind", "Kind"],
      ["validation", "Validation"],
      ["generator_record", "Generator"],
    ]);
    return;
  }
  if (state.activeDetailTab === "events") {
    const events = [
      ...(detail.controller_decisions || []).map((record) => ({ type: "controller", ...record })),
      ...(detail.engine_snapshots || []).map((record) => ({ type: "engine", ...record })),
      ...(detail.scheduler_events || []).map((record) => ({ type: "scheduler", ...record })),
    ];
    els.detailTabContent.innerHTML = tableFromRows(events, [
      ["type", "Type"],
      ["event", "Event"],
      ["engine_id", "Engine"],
      ["created_at", "Created"],
      ["reason", "Reason"],
    ]);
    return;
  }
  renderFilesTab(detail);
}

function renderFilesTab(detail) {
  const files = detail.files || [];
  els.detailTabContent.innerHTML = `
    <div class="list">
      ${files
        .map(
          (file) => `
            <button class="file-button" type="button" data-file-path="${escapeHtml(file.relative_path)}">
              ${escapeHtml(file.relative_path)}
              <span class="muted">${formatBytes(file.size)}</span>
            </button>
          `
        )
        .join("")}
    </div>
    <div id="filePreview"></div>
  `;
  document.querySelectorAll(".file-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await getJson(
        `/api/runs/${encodeURIComponent(detail.run.id)}/file?path=${encodeURIComponent(button.dataset.filePath)}`
      );
      document.getElementById("filePreview").innerHTML = `
        <h3>${escapeHtml(response.relative_path)}</h3>
        <pre class="code-box">${escapeHtml(response.content)}</pre>
      `;
    });
  });
}

function renderCatalog() {
  if (!state.catalog) return;
  renderCatalogList(els.environmentsList, state.catalog.environments || [], (item) => [
    item.summary.candidate_type,
    item.summary.evaluate_type,
    ...(item.summary.metrics || []),
  ]);
  renderCatalogList(els.methodsList, state.catalog.methods || [], (item) => [
    item.summary.controller,
    item.summary.engine,
    item.summary.batch_size ? `batch ${item.summary.batch_size}` : null,
  ]);
  renderCatalogList(els.studiesList, state.catalog.studies || [], (item) => [
    item.summary.objective && item.summary.objective.metric,
    item.summary.objective && item.summary.objective.direction,
    item.summary.budget && item.summary.budget.maxTrials ? `${item.summary.budget.maxTrials} trials` : null,
  ]);
  els.builtinsList.innerHTML = Object.entries(state.catalog.builtins || {})
    .map(
      ([category, values]) => `
        <div class="list-item">
          <h3>${escapeHtml(category)}</h3>
          <div class="item-meta">${values.map((value) => `<span class="tag">${escapeHtml(value)}</span>`).join("")}</div>
        </div>
      `
    )
    .join("");
}

function renderCatalogList(container, items, metaFn) {
  if (!items.length) {
    container.innerHTML = `<div class="empty-state"><p>No entries discovered.</p></div>`;
    return;
  }
  container.innerHTML = items
    .map((item) => {
      const meta = metaFn(item).filter(Boolean);
      return `
        <div class="list-item">
          <h3>${escapeHtml(item.label)}</h3>
          <p class="path-text">${escapeHtml(item.path)}</p>
          ${item.description ? `<p>${escapeHtml(item.description)}</p>` : ""}
          <div class="item-meta">
            ${item.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
            ${meta.map((value) => `<span class="tag">${escapeHtml(String(value))}</span>`).join("")}
          </div>
        </div>
      `;
    })
    .join("");
}

async function validateStudy() {
  const payload = { study_path: els.studyPathInput.value.trim() };
  const result = await postJson("/api/studies/validate", payload, { tolerateError: true });
  renderValidation(result);
}

async function launchStudy(event) {
  event.preventDefault();
  const payload = {
    study_path: els.studyPathInput.value.trim(),
    output_root: els.outputRootInput.value.trim(),
  };
  const result = await postJson("/api/studies/launch", payload, { tolerateError: true });
  if (result.job) {
    renderValidation({ valid: true, errors: [], name: "Launched", path: result.job.study_path });
    await loadRunsAndJobs();
    setView("studies");
    return;
  }
  renderValidation(result);
}

function renderValidation(result) {
  const valid = Boolean(result.valid);
  els.validationResult.innerHTML = `
    <div class="${valid ? "status-completed" : "status-failed"} status-pill">${valid ? "Valid" : "Invalid"}</div>
    <pre class="code-box">${escapeHtml(JSON.stringify(result, null, 2))}</pre>
  `;
}

function renderJobs() {
  if (!state.jobs.length) {
    els.jobsList.innerHTML = `<div class="empty-state"><p>No UI-launched jobs yet.</p></div>`;
    return;
  }
  els.jobsList.innerHTML = state.jobs
    .map(
      (job) => `
        <div class="list-item">
          <div class="detail-heading">
            <div>
              <h3>${escapeHtml(job.job_id)}</h3>
              <p class="path-text">${escapeHtml(job.study_path)}</p>
            </div>
            ${statusPill(job.status)}
          </div>
          <div class="item-meta">
            <span class="tag">pid ${escapeHtml(job.process_id || "-")}</span>
            <span class="tag">exit ${escapeHtml(job.exit_code ?? "-")}</span>
          </div>
          ${
            job.status === "running"
              ? `<button type="button" class="icon-button stop-job" data-job-id="${escapeHtml(job.job_id)}">Stop</button>`
              : ""
          }
        </div>
      `
    )
    .join("");
  document.querySelectorAll(".stop-job").forEach((button) => {
    button.addEventListener("click", async () => {
      await postJson(`/api/jobs/${encodeURIComponent(button.dataset.jobId)}/stop`, {});
      await loadRunsAndJobs();
    });
  });
}

function tableFromRows(rows, columns) {
  if (!rows.length) {
    return `<div class="empty-state"><p>No records found.</p></div>`;
  }
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows
            .map(
              (row) => `
                <tr>
                  ${columns.map(([key]) => `<td>${formatCell(row[key])}</td>`).join("")}
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function formatCell(value) {
  if (value == null || value === "") return "-";
  if (typeof value === "object") {
    return `<pre class="path-text">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
  }
  if (String(value).length > 80) {
    return `<span class="path-text">${escapeHtml(String(value))}</span>`;
  }
  return escapeHtml(String(value));
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
  return `<span class="status-pill status-${escapeHtml(status || "incomplete")}">${statusText(status)}</span>`;
}

function statusText(status) {
  return escapeHtml(status || "incomplete");
}

function setStatusClass(element, status) {
  element.className = `status-pill status-${status || "incomplete"}`;
}

function formatMetric(value) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(4).replace(/\.?0+$/, "");
}

function formatTime(value) {
  if (!value) return "-";
  if (typeof value === "number") return new Date(value * 1000).toLocaleString();
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? String(value) : new Date(parsed).toLocaleString();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function sum(values) {
  return values.reduce((total, value) => total + value, 0);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

