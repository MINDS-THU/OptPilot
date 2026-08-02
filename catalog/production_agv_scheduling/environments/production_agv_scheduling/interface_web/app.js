const elements = {
  additionalFiles: document.querySelector("#additional-files"),
  candidateSource: document.querySelector("#candidate-source"),
  disableFaults: document.querySelector("#disable-faults"),
  estimator: document.querySelector("#estimator-source"),
  eventProgress: document.querySelector("#event-progress"),
  horizon: document.querySelector("#horizon"),
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
};

let hasCompletedRun = false;

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
}

async function runCandidate() {
  clearError();
  elements.run.disabled = true;
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
    renderState(state);
  } catch (error) {
    showError(error);
  }
}

async function replayLast() {
  clearError();
  try {
    const state = await request("/api/replay", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({replay_speed: Number(elements.replaySpeed.value)}),
    });
    renderState(state);
  } catch (error) {
    showError(error);
  }
}

async function stopRun() {
  try {
    renderState(await request("/api/stop", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    }));
  } catch (error) {
    showError(error);
  }
}

function renderState(state) {
  const active = ["running", "waiting_for_viewer", "replaying"].includes(state.status);
  elements.status.textContent = state.status.replaceAll("_", " ");
  elements.status.className = `status-pill status-${state.status}`;
  elements.runId.textContent = state.run_id || "—";
  elements.eventProgress.textContent = `${state.events_published || 0}${state.event_count ? ` / ${state.event_count}` : ""} events`;
  elements.message.textContent = state.message || "";
  elements.run.disabled = active;
  elements.stop.disabled = !active;
  hasCompletedRun = hasCompletedRun || state.status === "completed";
  elements.replay.disabled = active || !hasCompletedRun;
  const score = state.result?.kpi?.total_score;
  elements.totalScore.textContent = typeof score === "number" ? score.toFixed(3) : "—";
  if (state.error) {
    elements.runError.hidden = false;
    elements.runError.textContent = state.error;
  }
}

function showError(error) {
  elements.runError.hidden = false;
  elements.runError.textContent = error instanceof Error ? error.message : String(error);
  elements.run.disabled = false;
}

function clearError() {
  elements.runError.hidden = true;
  elements.runError.textContent = "";
}

elements.run.addEventListener("click", runCandidate);
elements.replay.addEventListener("click", replayLast);
elements.stop.addEventListener("click", stopRun);

Promise.all([loadCandidate(), request("/api/state").then(renderState)])
  .catch(showError);

setInterval(async () => {
  try {
    renderState(await request("/api/state"));
  } catch (error) {
    showError(error);
  }
}, 750);
