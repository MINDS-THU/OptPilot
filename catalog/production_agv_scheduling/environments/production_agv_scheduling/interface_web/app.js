const elements = {
  additionalFiles: document.querySelector("#additional-files"),
  candidateSource: document.querySelector("#candidate-source"),
  disableFaults: document.querySelector("#disable-faults"),
  estimator: document.querySelector("#estimator-source"),
  eventProgress: document.querySelector("#event-progress"),
  horizon: document.querySelector("#horizon"),
  horizonHint: document.querySelector("#horizon-hint"),
  message: document.querySelector("#run-message"),
  replay: document.querySelector("#replay-button"),
  replaySpeed: document.querySelector("#replay-speed"),
  run: document.querySelector("#run-button"),
  runError: document.querySelector("#run-error"),
  runId: document.querySelector("#run-id"),
  scheduler: document.querySelector("#scheduler-source"),
  seed: document.querySelector("#seed"),
  status: document.querySelector("#viewer-status"),
  stop: document.querySelector("#stop-button"),
  timeStep: document.querySelector("#time-step"),
  totalScore: document.querySelector("#total-score"),
  unityViewer: document.querySelector("#unity-viewer"),
  viewerConnection: document.querySelector("#viewer-connection"),
};

let hasCompletedRun = false;
let actionPending = false;
let lastState = null;
let operationSerial = 0;
const MIN_MOTION_HORIZON = 11;

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function optionsPayload() {
  return {
    disable_faults: elements.disableFaults.checked,
    replay_speed: Number(elements.replaySpeed.value),
    seed: Number(elements.seed.value),
    simulation_horizon: Number(elements.horizon.value),
    time_step: Number(elements.timeStep.value),
  };
}

async function loadCandidate() {
  const payload = await request("/api/candidate");
  elements.scheduler.value = payload.candidate["scheduler.py"];
  elements.estimator.value = payload.candidate["param_estimator.py"];
  elements.seed.value = payload.defaults.seed;
  elements.horizon.value = payload.defaults.simulation_horizon;
  elements.timeStep.value = payload.defaults.time_step;
  elements.replaySpeed.value = String(payload.defaults.replay_speed);
  elements.disableFaults.checked = payload.defaults.disable_faults;
  elements.candidateSource.textContent = payload.source;
  elements.additionalFiles.textContent = payload.additional_files.length
    ? `${payload.additional_files.length} helper file(s) under policy/ are preserved when this candidate runs.`
    : "This candidate contains the two primary policy files.";
  updateHorizonHint();
}

async function runCandidate() {
  clearError();
  if (Number(elements.horizon.value) < MIN_MOTION_HORIZON) {
    showError(new Error(
      "This visual run is too short to contain AGV movement. Use a horizon of at least 11; 30 is recommended.",
    ));
    return;
  }
  if (actionPending) return;
  beginAction();
  try {
    const state = await request("/api/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        candidate: {
          "scheduler.py": elements.scheduler.value,
          "param_estimator.py": elements.estimator.value,
        },
        options: optionsPayload(),
      }),
    });
    resetUnityViewer(state.run_id);
    renderState(state);
  } catch (error) {
    showError(error);
  } finally {
    finishAction();
  }
}

async function replayLast() {
  clearError();
  if (actionPending) return;
  beginAction();
  try {
    const state = await request("/api/replay", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({replay_speed: Number(elements.replaySpeed.value)}),
    });
    resetUnityViewer(state.run_id);
    renderState(state);
  } catch (error) {
    showError(error);
  } finally {
    finishAction();
  }
}

async function stopRun() {
  if (actionPending) return;
  beginAction();
  try {
    const state = await request("/api/stop", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    renderState(state);
  } catch (error) {
    showError(error);
  } finally {
    finishAction();
  }
}

function resetUnityViewer(runId) {
  if (typeof runId !== "string" || !runId) return;
  const current = new URL(elements.unityViewer.src, window.location.href)
    .searchParams.get("generation");
  if (current !== runId) {
    elements.unityViewer.src = `/unity/?generation=${encodeURIComponent(runId)}`;
  }
}

function beginAction() {
  actionPending = true;
  operationSerial += 1;
  updateControls();
}

function finishAction() {
  actionPending = false;
  updateControls();
}

function updateControls() {
  const status = lastState?.status;
  const active = ["running", "waiting_for_viewer", "replaying"].includes(status);
  const viewerReady = lastState?.viewer_ready === true;
  elements.run.disabled = actionPending || active || !viewerReady;
  elements.stop.disabled = actionPending || !active;
  elements.replay.disabled = actionPending || active || !hasCompletedRun;
}

function renderState(state) {
  lastState = state;
  resetUnityViewer(state.run_id || "bootstrap");
  const active = ["running", "waiting_for_viewer", "replaying"].includes(state.status);
  const viewerReady = state.viewer_ready === true;
  elements.status.textContent = viewerReady
    ? state.status.replaceAll("_", " ")
    : "viewer loading";
  elements.status.className = `status-pill status-${viewerReady ? state.status : "waiting_for_viewer"}`;
  elements.viewerConnection.textContent = viewerReady ? "Ready" : "Loading…";
  elements.runId.textContent = state.run_id || "—";
  const simulationTime = typeof state.simulation_time === "number"
    ? ` · t=${Number(state.simulation_time.toFixed(2))}`
    : "";
  elements.eventProgress.textContent = `${state.events_published || 0}${state.event_count ? ` / ${state.event_count}` : ""} events${simulationTime}`;
  if (!viewerReady && !active) {
    elements.message.textContent = "Loading the 3D viewer. Run will be enabled when local telemetry is connected.";
  } else if (state.status === "running") {
    elements.message.textContent = "Evaluating offline. The animation starts after evaluation finishes.";
  } else {
    elements.message.textContent = state.message || "";
  }
  hasCompletedRun = hasCompletedRun || state.status === "completed";
  updateControls();
  const score = state.result?.kpi?.total_score;
  elements.totalScore.textContent = typeof score === "number" ? score.toFixed(3) : "—";
  if (state.error) {
    elements.runError.hidden = false;
    elements.runError.textContent = state.error;
  }
  return true;
}

function updateHorizonHint() {
  const horizon = Number(elements.horizon.value);
  if (Number.isFinite(horizon) && horizon < MIN_MOTION_HORIZON) {
    elements.horizonHint.hidden = false;
    elements.horizonHint.textContent = "No AGV movement occurs before simulation time 10.5. Use at least 11; 30 is recommended for a clearly visible replay.";
    return;
  }
  if (Number.isFinite(horizon) && horizon < 30) {
    elements.horizonHint.hidden = false;
    elements.horizonHint.textContent = "Only brief AGV movement may be visible at this horizon. Use 30 or more for a useful visual replay.";
    return;
  }
  elements.horizonHint.hidden = true;
  elements.horizonHint.textContent = "";
}

function showError(error) {
  elements.runError.hidden = false;
  elements.runError.textContent = error instanceof Error ? error.message : String(error);
  updateControls();
}

function clearError() {
  elements.runError.hidden = true;
  elements.runError.textContent = "";
}

elements.run.addEventListener("click", runCandidate);
elements.replay.addEventListener("click", replayLast);
elements.stop.addEventListener("click", stopRun);
elements.horizon.addEventListener("input", updateHorizonHint);

async function pollState() {
  const requestSerial = operationSerial;
  try {
    const state = await request("/api/state");
    if (!actionPending && requestSerial === operationSerial) renderState(state);
  } catch (error) {
    if (!actionPending && requestSerial === operationSerial) showError(error);
  } finally {
    window.setTimeout(pollState, 750);
  }
}

Promise.all([loadCandidate(), request("/api/state")])
  .then(([, state]) => {
    renderState(state);
    pollState();
  })
  .catch(showError);
