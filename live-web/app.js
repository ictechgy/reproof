import { decodeFramedStream } from "./stream.js";
import { PointerScheduler } from "./pointer.js";

const ACTIONS = ["tap", "long_press", "swipe", "text", "home", "reset"];
const params = new URLSearchParams(window.location.search);
const state = {
  devices: [],
  selectedDeviceId: null,
  session: null,
  frame: null,
  recording: null,
  replay: null,
  clientId: getClientId(),
  frameInFlight: false,
  frameGeneration: 0,
  frameTimestamps: [],
  sessionPollTimer: null,
  framePollTimer: null,
  streamTask: null,
  streamAbortController: null,
  streamGeneration: 0,
  streamFallback: false,
  streamBackoff: 0.5,
  frameDecodeBusy: false,
  pendingFrame: null,
  lastReceivedFrameId: null,
  sessionPollInFlight: false,
  sessionAbortController: null,
  frameAbortController: null,
  recordingFetchInFlight: false,
  recordingBusy: false,
  libraryRecordings: [],
  selectedRecording: null,
  recordingsFetchInFlight: false,
  jobs: [],
  repairs: [],
  repairEnabled: false,
  jobsPollTimer: null,
  jobsPollInFlight: false,
  jobBusy: false,
  liveSessions: [],
  sessionsPollTimer: null,
  sessionsPollInFlight: false,
  heartbeatTimer: null,
  inputQueue: Promise.resolve(),
  sequence: 0,
  pointerStart: null,
  pointerGestures: new Map(),
  pointerScheduler: null,
  timelineRenderKey: null,
  appLogs: null,
  appLogsStatus: "idle",
  appLogsError: "",
  appLogsCollectionFailed: false,
  appLogsExpanded: false,
  appLogsFetchInFlight: false,
  appLogsAbortController: null,
  appLogsDownloadAbortController: null,
  appLogsGeneration: 0,
  appLogsPollTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const refs = {
  connectionDot: $("#connection-dot"),
  connectionLabel: $("#connection-label"),
  clientLabel: $("#client-id-label"),
  deviceCount: $("#device-count"),
  deviceList: $("#device-list"),
  deviceSearch: $("#device-search"),
  refreshDevices: $("#refresh-devices"),
  stageTitle: $("#stage-title"),
  stageSubtitle: $("#stage-subtitle"),
  kindBadge: $("#kind-badge"),
  startSession: $("#start-session"),
  screenStatusDot: $("#screen-status-dot"),
  screenStatusLabel: $("#screen-status-label"),
  frameFps: $("#frame-fps"),
  frameMeta: $("#frame-meta"),
  screenStage: $("#screen-stage"),
  screenPlaceholder: $("#screen-placeholder"),
  placeholderTitle: $("#placeholder-title"),
  placeholderCopy: $("#placeholder-copy"),
  screenShell: $("#screen-shell"),
  liveScreen: $("#live-screen"),
  frameLoading: $("#frame-loading"),
  gestureOverlay: $("#gesture-overlay"),
  gestureLine: $("#gesture-line"),
  gesturePoint: $("#gesture-point"),
  controlState: $("#control-state"),
  gestureMode: $("#gesture-mode-label"),
  homeButton: $("#home-button"),
  resetButton: $("#reset-button"),
  textInput: $("#text-input"),
  textSubmit: $("#text-submit"),
  takeControl: $("#take-control"),
  closeSession: $("#close-session"),
  liveError: $("#live-error"),
  sessionId: $("#session-id"),
  controllerState: $("#controller-state"),
  streamState: $("#stream-state"),
  recordingIndicator: $("#recording-indicator"),
  recordingState: $("#recording-state"),
  recordStart: $("#record-start"),
  recordStop: $("#record-stop"),
  recordingDetail: $("#recording-detail"),
  replayState: $("#replay-state"),
  variablesForm: $("#variables-form"),
  replayStart: $("#replay-start"),
  replayCancel: $("#replay-cancel"),
  refreshRecordings: $("#refresh-recordings"),
  recordingImport: $("#recording-import"),
  libraryCount: $("#library-count"),
  recordingLibrary: $("#recording-library"),
  librarySelected: $("#library-selected"),
  librarySelectedLabel: $("#library-selected-label"),
  deriveSpeed: $("#derive-speed"),
  deriveRecording: $("#derive-recording"),
  jobHelp: $("#job-help"),
  jobForm: $("#job-form"),
  jobVariablesForm: $("#job-variables-form"),
  jobRepeats: $("#job-repeats"),
  jobTimeout: $("#job-timeout"),
  submitJob: $("#submit-job"),
  jobsCount: $("#jobs-count"),
  jobsList: $("#jobs-list"),
  repairSection: $("#repair-section"),
  repairCount: $("#repair-count"),
  repairList: $("#repair-list"),
  submitRepair: $("#submit-repair"),
  liveSessionsCount: $("#live-sessions-count"),
  liveSessionsList: $("#live-sessions-list"),
  appLogsSection: $("#app-logs-section"),
  appLogsState: $("#app-logs-state"),
  appLogsRefresh: $("#app-logs-refresh"),
  appLogsDownload: $("#app-logs-download"),
  appLogsSummary: $("#app-logs-summary"),
  appLogsList: $("#app-logs-list"),
  exportRecording: $("#export-recording"),
  exportScript: $("#export-script"),
  timeline: $("#timeline"),
  toastRegion: $("#toast-region"),
};

class ApiError extends Error {
  constructor(message, status = 0, code = "request_failed") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function getClientId() {
  const key = "reproof-live-client-id";
  let value = sessionStorage.getItem(key);
  if (!value) {
    value = typeof crypto?.randomUUID === "function"
      ? crypto.randomUUID()
      : `client-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
    sessionStorage.setItem(key, value);
  }
  return value;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

function compactId(value) {
  if (!value) return "—";
  return value.length > 20 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function sessionPath(id, suffix = "") {
  return `/api/sessions/${encodeURIComponent(id)}${suffix}`;
}

async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const body = response.status === 204 ? null : (contentType.includes("json") ? await response.json() : await response.text());
  if (!response.ok) {
    const problem = body?.error || {};
    throw new ApiError(problem.message || `Request failed (${response.status})`, response.status, problem.code || "request_failed");
  }
  return body;
}

async function request(path, options = {}) {
  return apiRequest(path, options);
}

function setConnection(status, label) {
  refs.connectionDot.className = `connection-dot ${status}`.trim();
  refs.connectionLabel.textContent = label;
}

function showError(message, options = {}) {
  refs.liveError.textContent = message;
  refs.liveError.hidden = false;
  if (!options.keep) window.setTimeout(() => { refs.liveError.hidden = true; }, options.timeout || 7000);
}

function clearError() {
  refs.liveError.hidden = true;
  refs.liveError.textContent = "";
}

function toast(message, type = "info") {
  const element = document.createElement("div");
  element.className = `toast ${type === "error" ? "error" : ""}`.trim();
  element.textContent = message;
  refs.toastRegion.append(element);
  window.setTimeout(() => element.remove(), 4200);
}

async function loadDevices() {
  refs.deviceList.innerHTML = '<div class="loading-card"><span class="spinner"></span><span>Loading devices…</span></div>';
  try {
    const result = await apiRequest("/api/devices");
    state.devices = Array.isArray(result?.devices) ? result.devices : [];
    setConnection("connected", "API connected");
    renderDevices();
  } catch (error) {
    state.devices = [];
    setConnection("error", "API unavailable");
    renderDevices();
    showError(`${error.message}. Start the local live-serve command and refresh.`, { keep: true });
  }
}

function filteredDevices() {
  const query = refs.deviceSearch.value.trim().toLowerCase();
  if (!query) return state.devices;
  return state.devices.filter((device) => `${device.name} ${device.platform} ${device.kind} ${device.id}`.toLowerCase().includes(query));
}

function deviceBadge(device) {
  const isDemo = String(device?.kind || "").toLowerCase().includes("demo") || device?.capabilities?.media === "demo-svg";
  return `<span class="device-kind">${isDemo ? "Demo" : escapeHtml(device.kind || "Simulator")}</span>`;
}

function renderDevices() {
  const devices = filteredDevices();
  refs.deviceCount.textContent = String(state.devices.length);
  if (!devices.length) {
    refs.deviceList.innerHTML = '<div class="empty-card">No matching devices.<br />Refresh when the farm is online.</div>';
    syncControls();
    return;
  }
  refs.deviceList.innerHTML = devices.map((device) => {
    const selected = device.id === state.selectedDeviceId;
    const stateName = device.state || "unknown";
    const stateLabel = stateName === "available" ? "Available" : stateName[0].toUpperCase() + stateName.slice(1);
    const actions = (device.capabilities?.actions || []).filter((action) => ACTIONS.includes(action)).slice(0, 4);
    const pointerCapable = device.capabilities?.inputMode === "continuous-pointer" &&
      Array.isArray(device.capabilities?.actions) && device.capabilities.actions.includes("pointer");
    const capabilityChips = [...actions, ...(pointerCapable ? ["live pointer"] : []),
      ...(pointerCapable && Number(device.capabilities?.maxPointers) > 1 ? [`multitouch ×${Math.min(5, Number(device.capabilities.maxPointers))}`] : [])];
    return `<button class="device-card ${selected ? "selected" : ""}" data-device-id="${escapeHtml(device.id)}" data-testid="device-card" type="button" aria-pressed="${selected}">
      <span class="device-card-top"><span class="device-name">${escapeHtml(device.name || device.id)}</span>${deviceBadge(device)}</span>
      <span class="device-card-bottom"><span class="platform-label">${escapeHtml(device.platform || "Unknown platform")}</span><span class="device-state ${escapeHtml(stateName)}"><i class="status-dot ${stateName === "available" ? "live" : stateName === "busy" ? "warning" : ""}" aria-hidden="true"></i>${stateLabel}</span></span>
      <span class="capability-row">${capabilityChips.map((action) => `<span class="mini-chip">${escapeHtml(action)}</span>`).join("") || '<span class="mini-chip">metadata only</span>'}</span>
    </button>`;
  }).join("");
  syncControls();
}

function selectedDevice() {
  return state.devices.find((device) => device.id === state.selectedDeviceId) || null;
}

function selectDevice(deviceId) {
  if (state.session && state.session.state !== "closed" && state.session.deviceId !== deviceId) {
    toast("Close the active session before switching devices.", "error");
    return;
  }
  state.selectedDeviceId = deviceId;
  clearError();
  renderDevices();
  renderSessionSurface();
}

function adoptSession(session) {
  if (!session) return;
  const epochChanged = state.session?.id === session.id && state.session?.epoch !== session.epoch;
  const sessionChanged = Boolean(state.session?.id && state.session.id !== session.id);
  if ((epochChanged || sessionChanged) && state.pointerScheduler?.size) cancelContinuousPointers();
  if (sessionChanged) resetAppLogs();
  if (state.session?.id !== session.id || state.session?.epoch !== session.epoch) state.sequence = session.lastSequence || 0;
  state.session = session;
  sessionStorage.setItem("reproof-live-session", session.id);
  if (session.controllerId === state.clientId) {
    state.sequence = Math.max(state.sequence, 0);
  } else if (!session.controllerId) {
    state.sequence = 0;
  }
  if (session.replay) state.replay = session.replay;
  if (session.state === "closed") {
    state.frameGeneration += 1;
    stopPolling();
    state.frame = null;
    refs.liveScreen.removeAttribute("src");
  }
  if (session.error) showError(session.error, { keep: true });
  const device = state.devices.find((item) => item.id === session.deviceId);
  if (device) {
    const repairing = state.repairs.some((job) => job.deviceId === session.deviceId && !["verified", "failed", "cancelled", "interrupted"].includes(job.state));
    device.state = session.state === "closed" ? (repairing ? "repairing" : "available") : session.state === "failed" ? "quarantined" : "busy";
    renderDevices();
  }
  renderSessionSurface();
  syncControls();
  if (state.appLogsExpanded && automaticAppLogsSupported()) loadAppLogs(false);
}

function isController() {
  return Boolean(state.session && state.session.controllerId === state.clientId);
}

function sessionActive() {
  return Boolean(state.session && state.session.state !== "closed");
}

function canInteract() {
  return state.session?.state === "active" && Boolean(state.frame) && isController() && state.session?.mode !== "replay" && state.session?.replay?.state !== "running";
}

function renderSessionSurface() {
  const device = selectedDevice();
  const session = state.session;
  const hasSession = sessionActive();
  refs.gestureMode.textContent = continuousPointerEnabled() ? "Direct touch" : "Gesture at release";
  refs.stageTitle.textContent = session ? (device?.name || "Live device") : (device?.name || "Select a device to begin");
  refs.stageSubtitle.textContent = session ? `${device?.platform || "Device"} · session ${compactId(session.id)}` : device ? "Ready to reserve a live session." : "A human or agent can take control of the same session.";
  refs.startSession.disabled = !device || hasSession || device.state !== "available";
  refs.startSession.textContent = hasSession ? "Session active" : "Start session";
  refs.kindBadge.textContent = device ? (String(device.kind || "simulator").toLowerCase().includes("demo") || device.capabilities?.media === "demo-svg" ? "Demo device" : `${device.kind || "Simulator"}`) : "No session";
  refs.kindBadge.className = `capability-badge ${device && (String(device.kind || "").toLowerCase().includes("demo") || device.capabilities?.media === "demo-svg") ? "demo" : device ? "real" : ""}`.trim();
  refs.screenPlaceholder.hidden = Boolean(hasSession && state.frame);
  refs.screenShell.classList.toggle("active", canInteract());
  refs.screenShell.setAttribute("aria-disabled", String(!canInteract()));
  refs.screenStatusDot.className = `status-dot ${hasSession ? (state.session.state === "active" ? "live" : "warning") : ""}`.trim();
  refs.screenStatusLabel.textContent = !session ? "Waiting for a session" : session.state === "closed" ? "Session closed" : session.state === "failed" ? "Session failed · close to clean up" : state.frame ? (canInteract() ? (continuousPointerEnabled() ? "Live pointer active" : "Manual control active") : "View only · take control") : "Starting device stream";
  refs.placeholderTitle.textContent = session?.state === "closed" ? "Session closed" : !device ? "No live device" : !session ? "Session not started" : "Waiting for first frame";
  refs.placeholderCopy.textContent = session?.state === "closed" ? "Start a session to use this device again." : !device ? "Choose an available device from the fleet." : !session ? "Start a session to reserve this device." : "The device is preparing a sampled frame stream.";
  refs.frameLoading.classList.toggle("visible", Boolean(hasSession && !state.frame));
  refs.sessionId.textContent = compactId(session?.id);
  refs.controllerState.textContent = !session || session.state === "closed" ? "Unclaimed" : isController() ? (continuousPointerEnabled() ? "You · live pointer" : "You · manual") : session.controllerId ? "Another client" : "Unclaimed";
  refs.streamState.textContent = !session || session.state === "closed" ? "Idle" : state.frame ? "Sampled frames" : "Starting";
  refs.controlState.textContent = !session ? "Take control to interact" : isController() ? (continuousPointerEnabled() ? "Live pointer input" : "Gesture at release") : session.controllerId ? "View only · another controller" : "Take control to interact";
  updateFrameMetadata();
  renderReplayState();
  renderRecordingState();
  renderAppLogs();
  syncLibraryActionState();
}

function syncControls() {
  const interactive = canInteract() && !state.recordingBusy;
  const active = sessionActive();
  refs.takeControl.disabled = !active || isController() || state.session?.mode === "replay";
  refs.takeControl.textContent = state.session?.mode === "replay" ? "Replay in progress" : isController() ? "You have control" : "Take control";
  refs.closeSession.disabled = !active || (!isController() && state.session?.state !== "failed");
  const actions = state.session?.capabilities?.actions || [];
  refs.homeButton.disabled = !interactive || !actions.includes("home");
  refs.resetButton.disabled = !interactive || !actions.includes("reset");
  refs.textInput.disabled = !interactive || !actions.includes("text");
  refs.textSubmit.disabled = !interactive || !actions.includes("text");
  refs.recordStart.lastChild.textContent = actions.includes("reset") ? "Reset & record" : "Record inputs";
  refs.recordStart.disabled = !interactive || Boolean(state.recording?.status === "recording");
  refs.recordStop.disabled = !interactive || state.recording?.status !== "recording";
  refs.submitRepair.disabled = !interactive || !state.repairEnabled || !state.session?.capabilities?.sdkCapture || state.recording?.status !== "complete" || !state.recording?.replayable;
  refs.replayStart.disabled = !interactive || !state.recording?.id || !state.recording.replayable || state.recording.status === "recording" || state.replay?.state === "running";
  refs.replayCancel.disabled = !active || state.replay?.state !== "running";
  refs.exportRecording.disabled = !state.recording?.id;
  refs.exportScript.disabled = !state.recording?.replayable || state.recording?.status !== "complete";
  refs.screenShell.classList.toggle("active", interactive);
}

async function startSession() {
  const device = selectedDevice();
  if (!device) return;
  clearError();
  refs.startSession.disabled = true;
  try {
    state.frame = null;
    state.frameGeneration += 1;
    state.recording = null;
    state.replay = null;
    refs.liveScreen.removeAttribute("src");
    const result = await request("/api/sessions", { method: "POST", body: JSON.stringify({ deviceId: device.id, clientId: state.clientId }) });
    adoptSession(result?.session || result);
    renderRecordingState();
    renderReplayState();
    startPolling();
    toast(`Session started on ${device.name || device.id}.`);
  } catch (error) {
    refs.startSession.disabled = false;
    showError(error.message);
  }
}

async function takeControl() {
  if (!state.session) return;
  try {
    const result = await request(sessionPath(state.session.id, "/control"), { method: "POST", body: JSON.stringify({ clientId: state.clientId, expectedEpoch: state.session.epoch, mode: "manual" }) });
    adoptSession(result?.session || result);
    state.sequence = 0;
    clearError();
    toast("Manual control granted.");
  } catch (error) {
    if (error.status === 409 || error.code === "stale_epoch") showError(`${error.message} Refresh the session and take control again.`);
    else showError(error.message);
  }
}

async function closeSession() {
  if (!state.session || (!isController() && state.session.state !== "failed")) return;
  try {
    const result = await request(sessionPath(state.session.id, "/close"), { method: "POST", body: JSON.stringify({ controllerId: state.clientId, epoch: state.session.epoch }) });
    adoptSession(result?.session || result);
    stopPolling();
    state.recording = null;
    state.replay = null;
    state.frame = null;
    refs.liveScreen.removeAttribute("src");
    toast("Session closed.");
  } catch (error) {
    showError(error.message);
  }
}

function startPolling() {
  stopPolling();
  pollSession();
  state.sessionPollTimer = window.setInterval(pollSession, 2000);
  startFrameStream();
}

function stopPolling() {
  if (state.pointerScheduler?.size) cancelContinuousPointers();
  state.pointerScheduler = null;
  state.pointerGestures.clear();
  if (state.sessionPollTimer) window.clearInterval(state.sessionPollTimer);
  if (state.framePollTimer) window.clearInterval(state.framePollTimer);
  state.sessionPollTimer = null;
  state.framePollTimer = null;
  state.sessionAbortController?.abort();
  state.frameAbortController?.abort();
  state.streamAbortController?.abort();
  state.streamGeneration += 1;
  state.streamTask = null;
  state.streamFallback = false;
  state.streamBackoff = 0.5;
  state.pendingFrame = null;
  state.lastReceivedFrameId = null;
  state.sessionAbortController = null;
  state.frameAbortController = null;
  state.streamAbortController = null;
}

async function pollSession() {
  if (!sessionActive() || state.sessionPollInFlight) {
    return;
  }
  state.sessionPollInFlight = true;
  const sessionId = state.session.id;
  const abortController = new AbortController();
  state.sessionAbortController = abortController;
  try {
    const result = await apiRequest(sessionPath(sessionId), { signal: abortController.signal });
    if (state.session?.id === sessionId) {
      adoptSession(result?.session || result);
      if (state.recording?.id) refreshRecording(false);
    }
  } catch (error) {
    if (error.name !== "AbortError" && error.status !== 404) setConnection("warning", "Session reconnecting");
  } finally {
    state.sessionPollInFlight = false;
    if (state.sessionAbortController === abortController) state.sessionAbortController = null;
  }
}

function startLegacyFramePolling(sessionId, generation) {
  if (state.framePollTimer) window.clearInterval(state.framePollTimer);
  state.streamFallback = true;
  state.framePollTimer = window.setInterval(() => pollFrame(sessionId, generation), 500);
  pollFrame(sessionId, generation);
}

function streamIsCurrent(sessionId, generation) {
  return state.session?.id === sessionId && state.session.state !== "closed" && state.streamGeneration === generation;
}

function sleepWithSignal(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    }, { once: true });
  });
}

function isExplicitStreamFallback(error) {
  return error?.streamFallback === true;
}

async function startFrameStream() {
  const sessionId = state.session?.id;
  if (!sessionId || state.streamTask) return;
  const generation = ++state.streamGeneration;
  const abortController = new AbortController();
  state.streamAbortController = abortController;
  const task = consumeFrameStream(sessionId, generation, abortController.signal)
    .catch((error) => {
      if (error.name !== "AbortError" && streamIsCurrent(sessionId, generation)) showError(error.message || "Frame stream stopped.");
    });
  state.streamTask = task;
  task.then(() => {
      if (state.streamAbortController === abortController) state.streamAbortController = null;
      if (state.streamTask === task) state.streamTask = null;
    }, () => {
      if (state.streamAbortController === abortController) state.streamAbortController = null;
      if (state.streamTask === task) state.streamTask = null;
    });
}

async function consumeFrameStream(sessionId, generation, signal) {
  while (streamIsCurrent(sessionId, generation) && !signal.aborted) {
    let response;
    try {
      response = await fetch(sessionPath(sessionId, "/stream"), {
        credentials: "same-origin",
        headers: { Accept: "application/x-repro-frames" },
        signal,
      });
      const contentType = response.headers.get("content-type") || "";
      if (response.status === 404 || (response.ok &&
          (!contentType.toLowerCase().startsWith("application/x-repro-frames") || !response.body))) {
        startLegacyFramePolling(sessionId, generation);
        return;
      }
      if (!response.ok) throw new Error(`Frame stream failed (${response.status})`);
      setConnection("connected", "API connected");
      let ended = false;
      for await (const message of decodeFramedStream(response.body)) {
        if (!streamIsCurrent(sessionId, generation)) return;
        if (message.type === "end") {
          ended = true;
          if (message.reason === "session_closed" || message.reason === "session_failed") {
            await pollSession();
            return;
          }
          break;
        }
        enqueueDecodedFrame(sessionId, generation, message);
      }
      if (!ended) throw new Error("Frame stream ended unexpectedly.");
      state.streamBackoff = 0.5;
    } catch (error) {
      if (error.name === "AbortError" || signal.aborted || !streamIsCurrent(sessionId, generation)) return;
      if (isExplicitStreamFallback(error)) {
        startLegacyFramePolling(sessionId, generation);
        return;
      }
      if (!streamIsCurrent(sessionId, generation)) return;
      setConnection("warning", "Frame stream reconnecting");
      await sleepWithSignal(state.streamBackoff * 1000, signal);
      state.streamBackoff = Math.min(state.streamBackoff * 2, 5);
    }
  }
}

function enqueueDecodedFrame(sessionId, generation, message) {
  if (!streamIsCurrent(sessionId, generation)) return;
  if (message.id === state.lastReceivedFrameId) return;
  state.lastReceivedFrameId = message.id;
  state.frameTimestamps.push(performance.now());
  state.frameTimestamps = state.frameTimestamps.filter((timestamp) => performance.now() - timestamp < 5000);
  updateFrameMetadata();
  const frame = { id: message.id, geometryVersion: message.geometryVersion, width: message.width, height: message.height,
    orientation: message.orientation, capturedAt: message.capturedAt, mime: message.mime, body: message.body };
  if (state.frameDecodeBusy) {
    state.pendingFrame = { sessionId, generation, frame };
    return;
  }
  decodeAndCommitFrame(sessionId, generation, frame);
}

function decodeAndCommitFrame(sessionId, generation, frame) {
  state.frameDecodeBusy = true;
  const image = refs.liveScreen;
  const objectUrl = URL.createObjectURL(new Blob([frame.body], { type: frame.mime }));
  const commit = async () => {
    try {
      if (typeof image.decode === "function") await image.decode();
      if (!streamIsCurrent(sessionId, generation)) return;
      state.frame = frame;
      delete state.frame.body;
      refs.frameLoading.classList.remove("visible");
      renderSessionSurface();
    } catch (error) {
      if (streamIsCurrent(sessionId, generation)) showError("The sampled frame could not be decoded.");
    } finally {
      URL.revokeObjectURL(objectUrl);
      state.frameDecodeBusy = false;
      const pending = state.pendingFrame;
      state.pendingFrame = null;
      if (pending && streamIsCurrent(pending.sessionId, pending.generation)) {
        decodeAndCommitFrame(pending.sessionId, pending.generation, pending.frame);
      }
    }
  };
  image.onload = commit;
  image.onerror = () => commit();
  image.src = objectUrl;
}

async function pollFrame(sessionId = state.session?.id, generation = state.streamGeneration) {
  if (!sessionId || !streamIsCurrent(sessionId, generation) || state.frameInFlight) return;
  if (!sessionActive() || state.frameInFlight) return;
  state.frameInFlight = true;
  const abortController = new AbortController();
  state.frameAbortController = abortController;
  try {
    const result = await apiRequest(sessionPath(sessionId, "/frame"), { signal: abortController.signal });
    if (!result || !streamIsCurrent(sessionId, generation) || !result.imageBase64) return;
    if (result.id === state.frame?.id) return;
    const frame = { id: result.id, geometryVersion: result.geometryVersion, width: result.width, height: result.height, orientation: result.orientation, capturedAt: result.capturedAt, mime: result.mime, imageBase64: result.imageBase64 };
    const bytes = Uint8Array.from(atob(result.imageBase64), (character) => character.charCodeAt(0));
    if (state.frameDecodeBusy) {
      state.pendingFrame = { sessionId, generation,
        frame: { ...frame, body: bytes, mime: result.mime || "image/jpeg" } };
      return;
    }
    state.lastReceivedFrameId = result.id;
    state.frameTimestamps.push(performance.now());
    state.frameTimestamps = state.frameTimestamps.filter((timestamp) => performance.now() - timestamp < 5000);
    decodeAndCommitFrame(sessionId, generation, { ...frame, body: bytes, mime: result.mime || "image/jpeg" });
  } catch (error) {
    if (error.name !== "AbortError" && ![204, 404, 503].includes(error.status)) showError(error.message);
  } finally {
    state.frameInFlight = false;
    if (state.frameAbortController === abortController) state.frameAbortController = null;
  }
}

function updateFrameMetadata() {
  if (!state.frame) {
    refs.frameFps.textContent = "— received FPS";
    refs.frameMeta.textContent = "No frame";
    return;
  }
  const now = performance.now();
  state.frameTimestamps = state.frameTimestamps.filter((time) => now - time < 5000);
  const fps = state.frameTimestamps.length > 1 ? ((state.frameTimestamps.length - 1) / Math.max((now - state.frameTimestamps[0]) / 1000, 1)).toFixed(1) : "—";
  refs.frameFps.textContent = `${fps} received FPS`;
  refs.frameMeta.textContent = `${state.frame.width}×${state.frame.height} · ${state.frame.orientation || "portrait"}`;
}

function normalizePoint(event) {
  const content = imageContentRect();
  return { x: Math.max(0, Math.min(1, (event.clientX - content.left) / content.contentWidth)),
    y: Math.max(0, Math.min(1, (event.clientY - content.top) / content.contentHeight)) };
}

function imageContentRect() {
  const shell = refs.screenShell.getBoundingClientRect();
  const width = state.frame?.width || shell.width;
  const height = state.frame?.height || shell.height;
  const scale = Math.min(shell.width / width, shell.height / height);
  const contentWidth = width * scale;
  const contentHeight = height * scale;
  return { shell, contentWidth, contentHeight,
    left: shell.left + (shell.width - contentWidth) / 2,
    top: shell.top + (shell.height - contentHeight) / 2 };
}

function pointDistance(first, second) {
  const content = imageContentRect();
  return Math.hypot((first.x - second.x) * content.contentWidth, (first.y - second.y) * content.contentHeight);
}

function renderGesturePreview(start, end = start, show = true) {
  const content = imageContentRect();
  const x1 = content.left - content.shell.left + start.x * content.contentWidth;
  const y1 = content.top - content.shell.top + start.y * content.contentHeight;
  const x2 = content.left - content.shell.left + end.x * content.contentWidth;
  const y2 = content.top - content.shell.top + end.y * content.contentHeight;
  const length = Math.hypot(x2 - x1, y2 - y1);
  const angle = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
  refs.gesturePoint.style.left = `${(x1 / content.shell.width) * 100}%`;
  refs.gesturePoint.style.top = `${(y1 / content.shell.height) * 100}%`;
  refs.gestureLine.style.left = `${(x1 / content.shell.width) * 100}%`;
  refs.gestureLine.style.top = `${(y1 / content.shell.height) * 100}%`;
  refs.gestureLine.style.width = `${length}px`;
  refs.gestureLine.style.transform = `rotate(${angle}deg)`;
  refs.gestureOverlay.classList.toggle("show", show);
}

function clearGesturePreview() {
  refs.gestureOverlay.classList.remove("show");
  refs.gestureLine.style.width = "0";
}

function continuousPointerEnabled() {
  const capabilities = state.session?.capabilities;
  return capabilities?.inputMode === "continuous-pointer" &&
    Array.isArray(capabilities.actions) && capabilities.actions.includes("pointer") &&
    capabilities.maxPointers >= 1;
}

function pointerPacket(phase, point, metadata = {}) {
  const session = state.session;
  const frame = state.frame;
  return { sessionId: session.id, controllerId: session.controllerId, epoch: session.epoch,
    frameId: metadata.frameId ?? frame?.id ?? 0, geometryVersion: metadata.geometryVersion ?? frame?.geometryVersion ?? 0,
    x: round(point.x), y: round(point.y), phase };
}

function sendPointerPacket(packet) {
  const job = state.inputQueue.catch(() => undefined).then(() => sendPointerNow(packet));
  state.inputQueue = job.catch(() => undefined);
  return job;
}

async function sendPointerNow(packet) {
  if (!state.session || ((state.session.id !== packet.sessionId || state.session.epoch !== packet.epoch) && packet.phase !== "cancel")) return;
  const body = { controllerId: packet.controllerId, epoch: packet.epoch, sequence: ++state.sequence,
    commandId: typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `pointer-${Date.now()}-${state.sequence}`,
    frameId: packet.frameId, geometryVersion: packet.geometryVersion, action: "pointer",
    payload: { phase: packet.phase, pointerId: packet.pointerId, x: packet.x, y: packet.y } };
  try {
    const result = await request(sessionPath(packet.sessionId, "/input"), { method: "POST", body: JSON.stringify(body) });
    if (result?.session && state.session?.id === packet.sessionId && state.session?.epoch === packet.epoch) adoptSession(result.session);
  } catch (error) {
    if (error.status === 409 || error.code === "stale_epoch") showError(`${error.message} Take control again to refresh the session epoch.`);
    else showError(error.message);
    throw error;
  }
}

function ensurePointerScheduler() {
  if (state.pointerScheduler) return state.pointerScheduler;
  const maxPointers = Math.max(1, Math.min(5, Number(state.session?.capabilities?.maxPointers) || 1));
  state.pointerScheduler = new PointerScheduler({
    maxPointers,
    maxHz: 30,
    send: sendPointerPacket,
    onError: (error) => {
      showError(error.message || "Pointer input failed.");
      if (!state.pointerScheduler?.cancelling) cancelContinuousPointers();
    },
  });
  return state.pointerScheduler;
}

function cancelContinuousPointers() {
  if (!state.pointerScheduler || !state.pointerScheduler.size || !state.session) {
    state.pointerGestures.clear();
    return Promise.resolve();
  }
  const session = { ...state.session };
  const frame = state.frame ? { ...state.frame } : {};
  const scheduler = state.pointerScheduler;
  const task = scheduler.cancelAll((gesture) => ({
    sessionId: session.id, controllerId: session.controllerId, epoch: session.epoch,
    frameId: frame.id || 0, geometryVersion: frame.geometryVersion || 0, x: 0, y: 0,
    phase: "cancel", pointerId: gesture.slot,
  }));
  state.pointerGestures.clear();
  clearGesturePreview();
  return task;
}

function onPointerDown(event) {
  if (continuousPointerEnabled()) {
    if (state.recordingBusy || !canInteract() || !state.frame || event.button !== 0) return;
    const point = normalizePoint(event);
    const packet = pointerPacket("down", point);
    if (!ensurePointerScheduler().pointerDown(event.pointerId, packet)) return;
    state.pointerGestures.set(event.pointerId, { point, sessionId: packet.sessionId, epoch: packet.epoch,
      frameId: packet.frameId, geometryVersion: packet.geometryVersion });
    refs.screenShell.setPointerCapture?.(event.pointerId);
    if (state.pointerGestures.size === 1) renderGesturePreview(point);
    return;
  }
  if (state.recordingBusy || !canInteract() || !state.frame || state.pointerStart || event.button !== 0) return;
  const point = normalizePoint(event);
  state.pointerStart = { pointerId: event.pointerId, point, startedAt: performance.now(), frameId: state.frame.id, geometryVersion: state.frame.geometryVersion };
  refs.screenShell.setPointerCapture?.(event.pointerId);
  renderGesturePreview(point);
}

function onPointerMove(event) {
  if (continuousPointerEnabled()) {
    const gesture = state.pointerGestures.get(event.pointerId);
    if (!gesture || !state.frame) return;
    if (gesture.sessionId !== state.session?.id || gesture.epoch !== state.session?.epoch || gesture.geometryVersion !== state.frame.geometryVersion) {
      cancelContinuousPointers();
      return;
    }
    const point = normalizePoint(event);
    ensurePointerScheduler().pointerMove(event.pointerId, pointerPacket("move", point));
    if (state.pointerGestures.size === 1) renderGesturePreview(gesture.point, point);
    return;
  }
  if (!state.pointerStart || state.pointerStart.pointerId !== event.pointerId) return;
  renderGesturePreview(state.pointerStart.point, normalizePoint(event));
}

function onPointerUp(event) {
  if (continuousPointerEnabled()) {
    const gesture = state.pointerGestures.get(event.pointerId);
    if (!gesture || !state.frame) return;
    const point = normalizePoint(event);
    if (gesture.sessionId === state.session?.id && gesture.epoch === state.session?.epoch && gesture.geometryVersion === state.frame.geometryVersion) {
      ensurePointerScheduler().pointerUp(event.pointerId, pointerPacket("up", point));
    } else {
      cancelContinuousPointers();
    }
    state.pointerGestures.delete(event.pointerId);
    if (!state.pointerGestures.size) clearGesturePreview();
    refs.screenShell.releasePointerCapture?.(event.pointerId);
    return;
  }
  if (!state.pointerStart || state.pointerStart.pointerId !== event.pointerId) return;
  const start = state.pointerStart;
  const end = normalizePoint(event);
  const durationMs = Math.max(50, Math.min(3000, Math.round(performance.now() - start.startedAt)));
  const distance = pointDistance(start.point, end);
  state.pointerStart = null;
  clearGesturePreview();
  const still = distance < 14;
  if (still && durationMs > 450) queueInput("long_press", { x: round(start.point.x), y: round(start.point.y), durationMs }, start);
  else if (still) queueInput("tap", { x: round(start.point.x), y: round(start.point.y) }, start);
  else queueInput("swipe", { fromX: round(start.point.x), fromY: round(start.point.y), toX: round(end.x), toY: round(end.y), durationMs }, start);
}

function onPointerCancel(event) {
  if (continuousPointerEnabled()) {
    // Releasing capture after a normal up is expected; only an active lost
    // pointer cancels the gesture (including its other fingers).
    if (state.pointerGestures.has(event.pointerId)) cancelContinuousPointers();
    return;
  }
  if (!state.pointerStart || state.pointerStart.pointerId !== event.pointerId) return;
  state.pointerStart = null;
  clearGesturePreview();
}

function round(value) { return Number(value.toFixed(4)); }

function queueInput(action, payload, metadata = {}) {
  if (state.recordingBusy || !canInteract()) {
    toast("Take control before sending an input.", "error");
    return Promise.resolve();
  }
  const sessionId = state.session.id;
  const epoch = state.session.epoch;
  const job = state.inputQueue.catch(() => undefined).then(() => {
    if (state.session?.id !== sessionId || state.session?.epoch !== epoch) return;
    return sendInput(action, payload, metadata);
  });
  state.inputQueue = job.catch(() => undefined);
  return job;
}

async function sendInput(action, payload, metadata = {}) {
  if (!canInteract()) return;
  const session = state.session;
  const body = { controllerId: session.controllerId, epoch: session.epoch, sequence: ++state.sequence, commandId: typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `command-${Date.now()}-${state.sequence}`, frameId: metadata.frameId || state.frame?.id || "", geometryVersion: metadata.geometryVersion ?? state.frame?.geometryVersion ?? 0, action, payload };
  try {
    const result = await request(sessionPath(session.id, "/input"), { method: "POST", body: JSON.stringify(body) });
    if (result?.session) adoptSession(result.session);
    toast(`${action.replace("_", " ")} injected`);
  } catch (error) {
    if (error.status === 409 || error.code === "stale_epoch") {
      state.sequence = Math.max(state.sequence, state.session?.lastSequence || 0);
      showError(`${error.message} Take control again to refresh the session epoch.`);
    } else showError(error.message);
  }
}

async function sendText() {
  const text = refs.textInput.value;
  if (!text || !canInteract()) return;
  await queueInput("text", { value: text }, { frameId: state.frame?.id, geometryVersion: state.frame?.geometryVersion });
  refs.textInput.value = "";
}

async function startRecording() {
  if (!state.session || !isController() || state.recordingBusy) return;
  state.recordingBusy = true;
  syncControls();
  try {
    await cancelContinuousPointers();
    await state.pointerScheduler?.drain();
    await state.inputQueue;
    const result = await request(sessionPath(state.session.id, "/recordings/start"), { method: "POST", body: JSON.stringify({ controllerId: state.clientId, epoch: state.session.epoch, reset: (state.session.capabilities?.actions || []).includes("reset") }) });
    state.recording = result?.recording || result;
    state.replay = null;
    renderRecordingState();
    syncControls();
    toast((state.session.capabilities?.actions || []).includes("reset") ? "Recording started after app reset." : "Recording started without a replayable reset fixture.");
  } catch (error) { showError(error.message); }
  finally { state.recordingBusy = false; syncControls(); }
}

async function stopRecording() {
  if (!state.session || !isController() || state.recordingBusy) return;
  state.recordingBusy = true;
  syncControls();
  try {
    await cancelContinuousPointers();
    await state.pointerScheduler?.drain();
    await state.pointerScheduler?.drain();
    await state.inputQueue;
    const result = await request(sessionPath(state.session.id, "/recordings/stop"), { method: "POST", body: JSON.stringify({ controllerId: state.clientId, epoch: state.session.epoch }) });
    state.recording = result?.recording || result;
    await refreshRecording(true);
    await loadRecordings(true);
    renderRecordingState();
    syncControls();
    toast("Recording stopped and ready to replay.");
  } catch (error) { showError(error.message); }
  finally { state.recordingBusy = false; syncControls(); }
}

async function refreshRecording(showErrors = false) {
  const id = state.recording?.id || state.session?.recordingId;
  if (!id || state.recordingFetchInFlight) return;
  state.recordingFetchInFlight = true;
  try {
    const result = await apiRequest(`/api/recordings/${encodeURIComponent(id)}`);
    state.recording = result?.recording || result;
    renderRecordingState();
    syncControls();
  } catch (error) {
    if (showErrors) showError(error.message);
  } finally { state.recordingFetchInFlight = false; }
}

function renderRecordingState() {
  const recording = state.recording;
  const status = recording?.status || "No recording";
  refs.recordingState.textContent = status;
  refs.recordingState.className = `soft-badge ${status === "recording" ? "active" : status === "complete" || status === "stopped" ? "success" : ""}`.trim();
  refs.recordingIndicator.hidden = status !== "recording";
  refs.recordingDetail.textContent = recording ? `${recording.events?.length || 0} events · ${recording.id ? compactId(recording.id) : "unsaved"}` : "Records gestures, timing, and frame references for replay.";
  renderVariablesForm();
  renderTimeline();
}

function renderVariablesForm() {
  const variables = Array.isArray(state.recording?.variables) ? state.recording.variables : [];
  if (!variables.length) { refs.variablesForm.hidden = true; refs.variablesForm.innerHTML = ""; return; }
  const key = `${state.recording.id}:${variables.join(",")}`;
  if (refs.variablesForm.dataset.key === key && refs.variablesForm.children.length) return;
  refs.variablesForm.dataset.key = key;
  refs.variablesForm.hidden = false;
  refs.variablesForm.innerHTML = `<p>This replay has variables. Values are used once and never saved.</p>${variables.map((name) => `<label class="variable-field">${escapeHtml(name)}<input type="text" data-variable="${escapeHtml(name)}" autocomplete="off" /></label>`).join("")}`;
}

function renderReplayState() {
  const replay = state.replay || state.session?.replay;
  const status = replay?.state || "Ready";
  refs.replayState.textContent = replay?.total ? `${status} · ${replay.index || 0}/${replay.total}` : status;
  refs.replayState.className = `soft-badge ${status === "running" ? "active" : status === "actions_replayed" ? "success" : ""}`.trim();
  syncControls();
}

function renderTimeline() {
  const events = state.recording?.events || [];
  const lastEvent = events[events.length - 1];
  const renderKey = [state.recording?.id || "", state.recording?.status || "", events.length,
    lastEvent?.id || "", state.recording?.digest || ""].join("|");
  if (state.timelineRenderKey === renderKey) return;
  state.timelineRenderKey = renderKey;
  if (!events.length) {
    refs.timeline.innerHTML = '<div class="empty-timeline"><span class="empty-line"></span><p>Record a session to see events here.</p></div>';
    return;
  }
  refs.timeline.innerHTML = events.map((event) => {
    const payload = event.payload ? JSON.stringify(redactPayload(event.action, event.payload)) : "";
    const offset = typeof event.offsetMs === "number" ? `+${(event.offsetMs / 1000).toFixed(2)}s` : "—";
    return `<div class="timeline-event"><span class="event-icon" aria-hidden="true"></span><div class="event-main"><strong>${escapeHtml(event.action || "event")}</strong><small>${escapeHtml(payload)}</small></div><span class="event-time">${escapeHtml(offset)}</span>${event.status ? `<span class="event-status">${escapeHtml(event.status)}</span>` : ""}</div>`;
  }).join("");
}

function redactPayload(action, payload) {
  if (action === "text" || Object.prototype.hasOwnProperty.call(payload || {}, "text")) return { variable: payload?.variable || "text" };
  return payload;
}

const APP_LOG_EVENT_LIMIT = 200;

function automaticAppLogsSupported() {
  return state.session?.capabilities?.automaticAppLogs === true;
}

function resetAppLogs() {
  state.appLogsGeneration += 1;
  state.appLogsAbortController?.abort();
  state.appLogsAbortController = null;
  state.appLogsDownloadAbortController?.abort();
  state.appLogsDownloadAbortController = null;
  state.appLogsFetchInFlight = false;
  state.appLogs = null;
  state.appLogsStatus = "idle";
  state.appLogsError = "";
  state.appLogsCollectionFailed = false;
  if (state.appLogsPollTimer) window.clearInterval(state.appLogsPollTimer);
  state.appLogsPollTimer = null;
}

function formatAppLogTime(event) {
  const elapsed = Number(event?.elapsedMs);
  if (!Number.isFinite(elapsed) || elapsed < 0) return "—";
  if (elapsed < 1000) return `+${Math.round(elapsed)}ms`;
  return `+${(elapsed / 1000).toFixed(2)}s`;
}

function appLogEventLabel(event) {
  const type = typeof event?.type === "string" ? event.type : "event";
  const name = typeof event?.name === "string" ? event.name : "unknown";
  return `${type} · ${name}`;
}

function renderAppLogs() {
  const supported = automaticAppLogsSupported();
  refs.appLogsSection.hidden = !supported;
  if (!supported) {
    syncAppLogsPolling();
    return;
  }
  const snapshot = state.appLogs;
  const events = Array.isArray(snapshot?.events) ? snapshot.events : [];
  const visible = events.slice(-APP_LOG_EVENT_LIMIT);
  const omitted = Math.max(0, events.length - visible.length);
  const stateLabel = state.appLogsStatus === "loading" ? "Loading" : state.appLogsStatus === "error" ? "Error" : snapshot ? "Ready" : state.appLogsStatus === "notready" ? "Not ready" : "Idle";
  refs.appLogsState.textContent = stateLabel;
  refs.appLogsState.className = `soft-badge ${state.appLogsStatus === "error" ? "error" : snapshot?.truncated || snapshot?.lostEvents ? "warning" : state.appLogsStatus === "loading" ? "active" : snapshot ? "success" : ""}`.trim();
  refs.appLogsRefresh.disabled = state.appLogsFetchInFlight;
  refs.appLogsDownload.disabled = !state.session?.id || !snapshot;
  const incompleteness = [];
  if (snapshot?.truncated) incompleteness.push("truncated");
  if (snapshot?.lostEvents) incompleteness.push("events lost");
  const incompleteLabel = incompleteness.length ? ` Incomplete log: ${incompleteness.join(" and ")}.` : "";
  const collectionWarning = state.appLogsCollectionFailed ? " Warning: the latest collection failed; this is the last saved prefix." : "";
  const refreshWarning = state.appLogsStatus === "error" ? ` Refresh failed${state.appLogsError ? `: ${state.appLogsError}` : "."}` : "";
  if (snapshot) {
    const first = events.length ? omitted + 1 : 0;
    refs.appLogsSummary.textContent = `Showing ${visible.length ? `${first}–${events.length}` : "0"} of ${events.length} observations.${incompleteLabel}${collectionWarning}${refreshWarning}`;
  } else if (state.appLogsStatus === "loading") {
    refs.appLogsSummary.textContent = "Reading the latest durable app log snapshot…";
  } else if (state.appLogsStatus === "notready") {
    refs.appLogsSummary.textContent = `Automatic app logs are not ready for this session. Refresh to try again.${collectionWarning}`;
  } else if (state.appLogsStatus === "error") {
    refs.appLogsSummary.textContent = `Could not read app logs${state.appLogsError ? `: ${state.appLogsError}` : "."} Refresh to try again.`;
  } else {
    refs.appLogsSummary.textContent = "Expand this section to read automatic observations.";
  }
  if (snapshot && visible.length) {
    refs.appLogsList.innerHTML = visible.map((event) => `<div class="app-log-event" role="listitem">
      <div class="app-log-event-top"><strong>${escapeHtml(appLogEventLabel(event))}</strong><span class="event-time">${escapeHtml(formatAppLogTime(event))}</span></div>
      <div class="app-log-event-meta"><span>target: ${escapeHtml(event?.target ?? "—")}</span><span>component: ${escapeHtml(event?.component || "—")}</span><span>hash: ${escapeHtml(event?.componentId || "—")}</span></div>
    </div>`).join("");
  } else if (state.appLogsStatus === "loading") {
    refs.appLogsList.innerHTML = '<div class="empty-card"><span class="spinner"></span><span>Waiting for an app log snapshot…</span></div>';
  } else if (state.appLogsStatus === "error") {
    refs.appLogsList.innerHTML = '<div class="empty-card">The previous snapshot remains available when present. Use Refresh to retry.</div>';
  } else if (state.appLogsStatus === "notready") {
    refs.appLogsList.innerHTML = '<div class="empty-card">The app has not published an automatic observation snapshot yet.</div>';
  } else {
    refs.appLogsList.innerHTML = '<div class="empty-card">No automatic observations yet.</div>';
  }
  syncAppLogsPolling();
}

function syncAppLogsPolling() {
  const shouldPoll = state.appLogsExpanded && sessionActive() && automaticAppLogsSupported();
  if (!shouldPoll) {
    if (state.appLogsPollTimer) window.clearInterval(state.appLogsPollTimer);
    state.appLogsPollTimer = null;
    return;
  }
  if (!state.appLogsPollTimer) state.appLogsPollTimer = window.setInterval(() => loadAppLogs(false), 5000);
}

async function loadAppLogs(showErrors = true) {
  if (!state.session?.id || !automaticAppLogsSupported() || state.appLogsFetchInFlight) return;
  const sessionId = state.session.id;
  const generation = state.appLogsGeneration;
  const abortController = new AbortController();
  state.appLogsFetchInFlight = true;
  state.appLogsAbortController = abortController;
  state.appLogsStatus = "loading";
  renderAppLogs();
  try {
    const result = await apiRequest(sessionPath(sessionId, "/app-logs"), { signal: abortController.signal });
    if (state.session?.id !== sessionId || state.appLogsGeneration !== generation) return;
    const snapshot = result?.appLog;
    state.appLogs = snapshot && typeof snapshot === "object" && !Array.isArray(snapshot) ? snapshot : null;
    state.appLogsStatus = state.appLogs ? "ready" : "notready";
    state.appLogsCollectionFailed = result?.collectionFailed === true;
    state.appLogsError = "";
    renderAppLogs();
  } catch (error) {
    if (error.name === "AbortError" || state.session?.id !== sessionId || state.appLogsGeneration !== generation) return;
    if (error.status === 409 || error.code === "app_log_unavailable" || error.code === "not_ready") {
      state.appLogsStatus = "notready";
      state.appLogsError = "";
      renderAppLogs();
      return;
    }
    state.appLogsStatus = "error";
    state.appLogsError = error.message || "request failed";
    renderAppLogs();
    if (showErrors) showError(`Could not load app logs: ${state.appLogsError}`);
  } finally {
    if (state.appLogsAbortController === abortController) {
      state.appLogsFetchInFlight = false;
      state.appLogsAbortController = null;
    }
    renderAppLogs();
  }
}

async function downloadAppLogs() {
  if (!state.session?.id || !automaticAppLogsSupported() || !state.appLogs) return;
  const sessionId = state.session.id;
  const generation = state.appLogsGeneration;
  state.appLogsDownloadAbortController?.abort();
  const abortController = new AbortController();
  state.appLogsDownloadAbortController = abortController;
  try {
    const response = await fetch(sessionPath(sessionId, "/app-logs/export"), { credentials: "same-origin", headers: { Accept: "application/json" }, signal: abortController.signal });
    if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
    const blob = await response.blob();
    if (state.session?.id !== sessionId || state.appLogsGeneration !== generation) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `app-logs-${sessionId}.json`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    if (error.name === "AbortError" || state.session?.id !== sessionId || state.appLogsGeneration !== generation) return;
    showError(`Could not download app logs: ${error.message}`);
  } finally {
    if (state.appLogsDownloadAbortController === abortController) state.appLogsDownloadAbortController = null;
  }
}

function recordingIsReplayable(recording) {
  return Boolean(recording && recording.status === "complete" && recording.replayable === true && !recording.invalid);
}

function recordingIsInvalid(recording) {
  return Boolean(recording?.invalid || recording?.valid === false || recording?.status === "invalid" || recording?.errorCode);
}

function recordingSource(recording) {
  return recording?.sourceDeviceName || recording?.deviceName || recording?.source?.deviceName || recording?.deviceId || "Unknown device";
}

async function loadRecordings(showErrors = false) {
  if (state.recordingsFetchInFlight) return;
  state.recordingsFetchInFlight = true;
  try {
    const result = await apiRequest("/api/recordings");
    state.libraryRecordings = Array.isArray(result?.recordings) ? result.recordings : [];
    if (state.selectedRecording?.id) {
      const refreshed = state.libraryRecordings.find((recording) => recording.id === state.selectedRecording.id);
      if (refreshed) state.selectedRecording = refreshed;
      else state.selectedRecording = null;
    }
    renderRecordingLibrary();
  } catch (error) {
    if (showErrors) showError(`Could not load recording library: ${error.message}`);
    if (!state.libraryRecordings.length) renderRecordingLibrary();
  } finally {
    state.recordingsFetchInFlight = false;
  }
}

function renderRecordingLibrary() {
  refs.libraryCount.textContent = String(state.libraryRecordings.length);
  if (!state.libraryRecordings.length) {
    refs.recordingLibrary.innerHTML = '<div class="empty-card">No frozen recordings yet.</div>';
  } else {
    refs.recordingLibrary.innerHTML = state.libraryRecordings.map((recording) => {
      const invalid = recordingIsInvalid(recording);
      const selected = state.selectedRecording?.id === recording.id;
      const replayable = recordingIsReplayable(recording);
      const frozen = ["complete", "invalid"].includes(recording.status);
      const stateLabel = invalid ? "Invalid" : replayable ? "Replayable" : "Incomplete";
      const canUse = canUseLibraryRecording(recording);
      return `<div class="library-item ${selected ? "selected" : ""} ${invalid ? "invalid" : ""}" data-recording-id="${escapeHtml(recording.id)}" role="group">
        <button class="library-item-select" data-select-recording="${escapeHtml(recording.id)}" type="button" aria-pressed="${selected}">
          <span class="library-item-top"><span class="library-item-name">${escapeHtml(recording.name || compactId(recording.id))}</span><span class="library-item-status ${invalid ? "invalid" : ""}">${stateLabel}</span></span>
        </button>
        <span class="library-item-meta">${escapeHtml(recordingSource(recording))} · ${Number(recording.events?.length || 0)} events · ${escapeHtml(recording.status || "frozen")}</span>
        <span class="library-item-actions">
          <button class="text-button" data-use-recording="${escapeHtml(recording.id)}" type="button" ${canUse ? "" : "disabled"}>Use in Live</button>
          <button class="text-button" data-library-export="json" type="button" ${frozen ? "" : "disabled"}>JSON</button>
          ${replayable ? `<button class="text-button" data-library-export="python" type="button">Python</button>` : ""}
        </span>
      </div>`;
    }).join("");
  }
  if (!state.selectedRecording) {
    refs.librarySelected.hidden = true;
  } else {
    refs.librarySelected.hidden = false;
    refs.librarySelectedLabel.textContent = `${state.selectedRecording.name || compactId(state.selectedRecording.id)} · ${recordingIsReplayable(state.selectedRecording) ? "replayable" : "not replayable"}`;
  }
  renderJobForm();
}

function canUseLibraryRecording(recording) {
  const recordingDeviceId = recording?.deviceId || recording?.source?.deviceId || recording?.sourceDeviceId;
  const replay = state.replay || state.session?.replay;
  return Boolean(recording && state.session?.state === "active" && isController() && recordingDeviceId && recordingDeviceId === state.session.deviceId && state.session.mode !== "replay" && replay?.state !== "running" && state.recording?.status !== "recording");
}

function syncLibraryActionState() {
  refs.recordingLibrary.querySelectorAll("[data-use-recording]").forEach((button) => {
    const recording = state.libraryRecordings.find((item) => item.id === button.dataset.useRecording);
    button.disabled = !canUseLibraryRecording(recording);
  });
}

function selectLibraryRecording(id) {
  state.selectedRecording = state.libraryRecordings.find((recording) => recording.id === id) || null;
  renderRecordingLibrary();
}

async function importRecording(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    showError("Recording JSON must be 5 MB or smaller.");
    return;
  }
  try {
    const parsed = JSON.parse(await file.text());
    const recording = parsed?.recording || parsed;
    if (!recording || typeof recording !== "object" || Array.isArray(recording) || !Array.isArray(recording.events)) {
      throw new Error("The file is not a frozen recording export.");
    }
    const hasRawLogs = Object.keys(recording).some((key) => key.toLowerCase() === "logs" || /^raw.?logs?$/.test(key.toLowerCase()));
    if (hasRawLogs) {
      throw new Error("Raw logs are not accepted in recording imports.");
    }
    const result = await request("/api/recordings/import", { method: "POST", body: JSON.stringify({ recording }) });
    const imported = result?.recording || recording;
    await loadRecordings(true);
    state.selectedRecording = state.libraryRecordings.find((item) => item.id === imported.id) || imported;
    renderRecordingLibrary();
    toast(result?.imported === false ? "Recording already in the library." : "Recording imported.", "info");
  } catch (error) {
    showError(`Import failed: ${error.message}`);
  }
}

async function deriveRecording() {
  const recording = state.selectedRecording;
  if (!recording?.id) return;
  const speed = Number(refs.deriveSpeed.value);
  if (![0.5, 1, 2].includes(speed)) return;
  refs.deriveRecording.disabled = true;
  try {
    const result = await request(`/api/recordings/${encodeURIComponent(recording.id)}/derive`, { method: "POST", body: JSON.stringify({ speed }) });
    const derived = result?.recording || result;
    await loadRecordings(true);
    state.selectedRecording = state.libraryRecordings.find((item) => item.id === derived.id) || derived;
    renderRecordingLibrary();
    toast("Derived recording copy created.");
  } catch (error) {
    showError(`Could not create recording copy: ${error.message}`);
  } finally {
    refs.deriveRecording.disabled = false;
  }
}

function renderJobForm() {
  const recording = state.selectedRecording;
  const ready = recordingIsReplayable(recording) && !recordingIsInvalid(recording);
  refs.jobForm.hidden = !ready;
  refs.jobHelp.textContent = !recording ? "Select a replayable library recording to queue a device-farm job." : ready ? `${recording.name || compactId(recording.id)} is ready for automation.` : "This recording is incomplete or invalid and cannot be queued.";
  if (!ready) {
    refs.jobVariablesForm.hidden = true;
    refs.jobVariablesForm.innerHTML = "";
    return;
  }
  const variables = Array.isArray(recording.variables) ? recording.variables : [];
  if (!variables.length) {
    refs.jobVariablesForm.hidden = true;
    refs.jobVariablesForm.innerHTML = "";
    return;
  }
  const key = `${recording.id}:${variables.join(",")}`;
  if (refs.jobVariablesForm.dataset.key === key && refs.jobVariablesForm.children.length) {
    refs.jobVariablesForm.hidden = false;
    return;
  }
  refs.jobVariablesForm.dataset.key = key;
  refs.jobVariablesForm.hidden = false;
  refs.jobVariablesForm.innerHTML = `<p>Values are used once and never saved.</p>${variables.map((name) => `<label class="variable-field">${escapeHtml(name)}<input type="text" data-job-variable="${escapeHtml(name)}" autocomplete="off" /></label>`).join("")}`;
}

function jobTerminal(job) {
  return ["succeeded", "failed", "cancelled", "interrupted"].includes(job?.state);
}

function renderJobs() {
  refs.jobsCount.textContent = String(state.jobs.length);
  if (!state.jobs.length) {
    refs.jobsList.innerHTML = '<div class="empty-card">No automation jobs yet.</div>';
    return;
  }
  refs.jobsList.innerHTML = state.jobs.slice(0, 12).map((job) => {
    const sessionLink = job.sessionId ? `?session=${encodeURIComponent(job.sessionId)}` : "";
    const canCancel = !jobTerminal(job);
    const error = job.errorCode ? ` · ${escapeHtml(job.errorCode)}` : "";
    return `<div class="job-item"><div class="job-item-top"><span class="job-item-name">${escapeHtml(compactId(job.id))}</span><span class="job-state ${escapeHtml(job.state || "queued")}">${escapeHtml(job.state || "queued")}</span></div>
      <span class="job-item-meta">${escapeHtml(compactId(job.recordingId))} · ${Number(job.completedRuns || 0)}/${Number(job.repeats || 1)} runs${error}</span>
      <div class="job-item-actions">${job.sessionId ? `<a class="text-button" href="${sessionLink}">Open session</a>` : ""}${canCancel ? `<button class="text-button" data-cancel-job="${escapeHtml(job.id)}" type="button">Cancel</button>` : ""}</div></div>`;
  }).join("");
}

async function loadJobs(showErrors = false) {
  if (state.jobsPollInFlight) return;
  state.jobsPollInFlight = true;
  try {
    const [result, repairs] = await Promise.all([apiRequest("/api/jobs"), apiRequest("/api/repairs")]);
    state.jobs = Array.isArray(result?.jobs) ? result.jobs : [];
    renderJobs();
    const previousRepairs = state.repairs;
    state.repairs = Array.isArray(repairs?.jobs) ? repairs.jobs : [];
    state.repairEnabled = repairs?.enabled === true;
    renderRepairs();
    if (state.repairs.some((job) => ["verified", "failed", "cancelled", "interrupted"].includes(job.state) &&
        previousRepairs.some((old) => old.id === job.id && !["verified", "failed", "cancelled", "interrupted"].includes(old.state)))) await loadDevices();
  } catch (error) {
    if (showErrors) showError(`Could not load automation jobs: ${error.message}`);
  } finally {
    state.jobsPollInFlight = false;
  }
}

function renderRepairs() {
  refs.repairSection.hidden = !state.repairEnabled;
  refs.repairCount.textContent = String(state.repairs.length);
  refs.repairList.innerHTML = state.repairs.map((job) => {
    const terminal = ["verified", "failed", "cancelled", "interrupted"].includes(job.state);
    const phases = { capturing: "Reading recorded evidence", handoff: "Reserving device", prepared: "Reproducing original bug", patching: "Generating and building fix", verifying: "Verifying patched app", finalizing: "Finishing device cleanup" };
    const summary = job.state === "verified" ? `${Number(job.baselineRuns)} reproduced · ${Number(job.verifiedRuns)} verified` : job.errorCode || phases[job.phase] || job.phase;
    return `<div class="job-item"><div class="job-item-top"><span class="job-item-name">${escapeHtml(job.case)}</span><span class="job-state">${escapeHtml(job.state)}</span></div><span class="job-item-meta">${escapeHtml(summary)}</span><div class="job-item-actions">${job.reportAvailable ? `<a class="text-button" href="/api/repairs/${encodeURIComponent(job.id)}/report" target="_blank" rel="noopener">Report</a>` : ""}${job.state === "verified" ? `<button class="text-button" data-resume-repair="${escapeHtml(job.id)}" type="button">Open repaired app</button>` : ""}${!terminal ? `<button class="text-button" data-cancel-repair="${escapeHtml(job.id)}" type="button">Cancel</button>` : ""}</div></div>`;
  }).join("");
}

async function submitRepair() {
  if (!state.session || !state.recording || refs.submitRepair.disabled) return;
  refs.submitRepair.disabled = true;
  try {
    await request(sessionPath(state.session.id, "/repair"), { method: "POST", body: JSON.stringify({
      controllerId: state.clientId, epoch: state.session.epoch, recordingId: state.recording.id,
      requestId: crypto.randomUUID() }) });
    toast("Repair started. The device is reserved while the original and patched app are verified.");
    await loadJobs(true);
  } catch (error) { showError(`Could not start repair: ${error.message}`); }
}

async function repairAction(id, action) {
  try {
    const result = await request(`/api/repairs/${encodeURIComponent(id)}/${action}`, {
      method: "POST", body: JSON.stringify(action === "resume" ? { clientId: state.clientId } : {}) });
    if (action === "resume") await attachSession(result.session.id);
    await loadJobs(true);
  } catch (error) { showError(`Could not ${action} repair: ${error.message}`); }
}

async function submitJob() {
  const recording = state.selectedRecording;
  if (!recording?.id || !recordingIsReplayable(recording) || state.jobBusy) return;
  const repeats = Number(refs.jobRepeats.value);
  if (!Number.isInteger(repeats) || repeats < 1 || repeats > 10) {
    showError("Repeats must be a whole number from 1 to 10.");
    refs.jobRepeats.focus();
    return;
  }
  const timeoutSeconds = Number(refs.jobTimeout.value);
  if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 900) {
    showError("Timeout must be a whole number from 1 to 900 seconds.");
    refs.jobTimeout.focus();
    return;
  }
  state.jobBusy = true;
  refs.submitJob.disabled = true;
  const variables = {};
  refs.jobVariablesForm.querySelectorAll("[data-job-variable]").forEach((input) => { variables[input.dataset.jobVariable] = input.value; });
  try {
    const result = await request("/api/jobs", { method: "POST", body: JSON.stringify({ recordingId: recording.id, variables, requestId: typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `job-${Date.now()}`, repeats, timeoutSeconds }) });
    const job = result?.job || result;
    state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
    renderJobs();
    refs.jobVariablesForm.querySelectorAll("input").forEach((input) => { input.value = ""; });
    toast(job.state === "queued" ? "Job queued; it will wait for a device if needed." : "Automation job submitted.");
  } catch (error) {
    if (error.status === 409 || ["device_busy", "no_device_available", "lease_busy"].includes(error.code)) showError("No device is free yet. The manual session stays open; try again when the farm releases one.");
    else showError(`Job submission failed: ${error.message}`);
  } finally {
    state.jobBusy = false;
    refs.submitJob.disabled = false;
  }
}

async function cancelJob(id) {
  const job = state.jobs.find((item) => item.id === id);
  if (!job || jobTerminal(job)) return;
  try {
    const result = await request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: JSON.stringify({}) });
    const updated = result?.job || result;
    state.jobs = state.jobs.map((item) => item.id === id ? updated : item);
    renderJobs();
    toast(updated.state === "cancelled" ? "Automation job cancelled." : "Cancellation requested; waiting for input cleanup.");
  } catch (error) { showError(`Could not cancel job: ${error.message}`); }
}

function useRecordingInLive(id) {
  const recording = state.libraryRecordings.find((item) => item.id === id);
  if (!recording) return;
  if (!canUseLibraryRecording(recording)) {
    showError("Use in Live requires an active manual session on the recording's device, with no recording or replay running.");
    return;
  }
  state.recording = recording;
  renderRecordingState();
  syncControls();
  toast("Recording selected for this live session.");
}

async function loadSessions(showErrors = false) {
  if (state.sessionsPollInFlight) return;
  state.sessionsPollInFlight = true;
  try {
    const result = await apiRequest("/api/sessions");
    state.liveSessions = Array.isArray(result?.sessions) ? result.sessions : [];
    renderLiveSessions();
  } catch (error) {
    if (showErrors) showError(`Could not load live sessions: ${error.message}`);
  } finally {
    state.sessionsPollInFlight = false;
  }
}

function renderLiveSessions() {
  const sessions = state.liveSessions.filter((session) => session.state !== "closed");
  refs.liveSessionsCount.textContent = String(sessions.length);
  if (!sessions.length) {
    refs.liveSessionsList.innerHTML = '<div class="empty-card">No active sessions.</div>';
    return;
  }
  refs.liveSessionsList.innerHTML = sessions.map((session) => {
    const current = state.session?.id === session.id;
    const stateLabel = session.state || "unknown";
    const leaseInfo = formatSessionLease(session);
    return `<div class="session-item ${current ? "current" : ""}"><div class="session-item-copy"><span class="session-item-name">${escapeHtml(session.deviceName || session.deviceId || "Device session")}</span><span class="session-item-meta">${escapeHtml(compactId(session.id))} · ${escapeHtml(stateLabel)}${leaseInfo}</span></div><button class="button button-secondary" data-attach-session="${escapeHtml(session.id)}" type="button">${current ? "Attached" : "Attach"}</button></div>`;
  }).join("");
}

function formatSessionLease(session) {
  if (session?.expiresAt !== undefined && session.expiresAt !== null) {
    const raw = session.expiresAt;
    const milliseconds = typeof raw === "number" ? raw : /^\d+$/.test(String(raw)) ? Number(raw) : Date.parse(String(raw));
    if (Number.isFinite(milliseconds)) {
      const remaining = Math.max(0, Math.round((milliseconds - Date.now()) / 1000));
      return ` · expires ${new Date(milliseconds).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} (${remaining}s)`;
    }
  }
  return session?.idleTimeoutSeconds ? ` · idle ${Number(session.idleTimeoutSeconds)}s` : "";
}

async function attachSession(sessionId) {
  if (state.session && sessionActive() && state.session.id !== sessionId && isController()) {
    toast("Close the active manual session before attaching another one.", "error");
    return;
  }
  const changingSession = Boolean(state.session?.id && state.session.id !== sessionId);
  if (changingSession) {
    stopPolling();
    state.frameGeneration += 1;
    state.frame = null;
    refs.liveScreen.removeAttribute("src");
    state.recording = null;
    state.replay = null;
  }
  try {
    const result = await apiRequest(sessionPath(sessionId));
    const session = result?.session || result;
    state.selectedDeviceId = session.deviceId;
    adoptSession(session);
    renderDevices();
    if (session.recordingId) await refreshRecording(true);
    startPolling();
    toast("Session attached. Take control when you are ready.");
  } catch (error) { showError(`Could not attach session: ${error.message}`); }
}

async function sendHeartbeat() {
  if (!sessionActive()) return;
  try {
    await apiRequest(sessionPath(state.session.id, "/heartbeat"), { method: "POST", body: JSON.stringify({ clientId: state.clientId }) });
  } catch (error) {
    if (error.status === 404) loadSessions();
  }
}

function startBackgroundPolling() {
  if (!state.jobsPollTimer) state.jobsPollTimer = window.setInterval(() => loadJobs(), 2000);
  if (!state.sessionsPollTimer) state.sessionsPollTimer = window.setInterval(() => loadSessions(), 5000);
  if (!state.heartbeatTimer) state.heartbeatTimer = window.setInterval(sendHeartbeat, 10000);
}

function stopBackgroundPolling() {
  if (state.jobsPollTimer) window.clearInterval(state.jobsPollTimer);
  if (state.sessionsPollTimer) window.clearInterval(state.sessionsPollTimer);
  if (state.heartbeatTimer) window.clearInterval(state.heartbeatTimer);
  state.jobsPollTimer = null;
  state.sessionsPollTimer = null;
  state.heartbeatTimer = null;
}

async function startReplay() {
  if (!state.session || !isController() || !state.recording?.id) return;
  const variables = {};
  refs.variablesForm.querySelectorAll("[data-variable]").forEach((input) => { variables[input.dataset.variable] = input.value; });
  try {
    const result = await request(sessionPath(state.session.id, "/replay"), { method: "POST", body: JSON.stringify({ controllerId: state.clientId, epoch: state.session.epoch, recordingId: state.recording.id, variables }) });
    state.replay = result?.replay || null;
    if (result?.session) adoptSession(result.session);
    renderReplayState();
    syncControls();
    refs.variablesForm.querySelectorAll("input").forEach((input) => { input.value = ""; });
    toast("Replay started.");
  } catch (error) { showError(error.message); }
}

async function cancelReplay() {
  if (!state.session || state.replay?.state !== "running") return;
  try {
    const result = await request(sessionPath(state.session.id, "/replay/cancel"), { method: "POST", body: JSON.stringify({ clientId: state.clientId }) });
    state.replay = result?.replay || null;
    if (result?.session) adoptSession(result.session);
    toast("Replay cancelled.");
  } catch (error) { showError(error.message); }
}

async function exportRecording(script = false, recording = state.recording) {
  if (!recording?.id) return;
  try {
    const response = await fetch(`/api/recordings/${encodeURIComponent(recording.id)}/${script ? "script" : "export"}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!response.ok) throw new ApiError(`Export failed (${response.status})`, response.status);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = script ? `replay-${recording.id}.py` : `recording-${recording.id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    toast("Recording exported.");
  } catch (error) { showError(error.message); }
}

function bindEvents() {
  refs.submitRepair.addEventListener("click", submitRepair);
  refs.repairList.addEventListener("click", (event) => {
    const resume = event.target.closest("[data-resume-repair]");
    const cancel = event.target.closest("[data-cancel-repair]");
    if (resume) repairAction(resume.dataset.resumeRepair, "resume");
    if (cancel) repairAction(cancel.dataset.cancelRepair, "cancel");
  });
  refs.refreshDevices.addEventListener("click", loadDevices);
  refs.deviceSearch.addEventListener("input", renderDevices);
  refs.deviceList.addEventListener("click", (event) => {
    const card = event.target.closest("[data-device-id]");
    if (card) selectDevice(card.dataset.deviceId);
  });
  refs.startSession.addEventListener("click", startSession);
  refs.takeControl.addEventListener("click", takeControl);
  refs.closeSession.addEventListener("click", closeSession);
  refs.homeButton.addEventListener("click", () => queueInput("home", {}));
  refs.resetButton.addEventListener("click", () => queueInput("reset", {}));
  refs.textSubmit.addEventListener("click", sendText);
  refs.textInput.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); sendText(); } });
  refs.recordStart.addEventListener("click", startRecording);
  refs.recordStop.addEventListener("click", stopRecording);
  refs.replayStart.addEventListener("click", startReplay);
  refs.replayCancel.addEventListener("click", cancelReplay);
  refs.exportRecording.addEventListener("click", () => exportRecording());
  refs.exportScript.addEventListener("click", () => exportRecording(true));
  refs.refreshRecordings.addEventListener("click", () => loadRecordings(true));
  refs.recordingImport.addEventListener("change", importRecording);
  refs.recordingLibrary.addEventListener("click", (event) => {
    const select = event.target.closest("[data-select-recording]");
    if (select) {
      selectLibraryRecording(select.dataset.selectRecording);
      return;
    }
    const use = event.target.closest("[data-use-recording]");
    if (use) {
      useRecordingInLive(use.dataset.useRecording);
      return;
    }
    const exportButton = event.target.closest("[data-library-export]");
    if (exportButton) {
      const item = exportButton.closest("[data-recording-id]");
      const recording = state.libraryRecordings.find((entry) => entry.id === item?.dataset.recordingId);
      exportRecording(exportButton.dataset.libraryExport === "python", recording);
    }
  });
  refs.deriveRecording.addEventListener("click", deriveRecording);
  refs.submitJob.addEventListener("click", submitJob);
  refs.jobsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cancel-job]");
    if (button) cancelJob(button.dataset.cancelJob);
  });
  refs.liveSessionsList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-attach-session]");
    if (button) attachSession(button.dataset.attachSession);
  });
  refs.appLogsSection.addEventListener("toggle", () => {
    state.appLogsExpanded = refs.appLogsSection.open;
    if (state.appLogsExpanded) loadAppLogs(true);
    syncAppLogsPolling();
  });
  refs.appLogsRefresh.addEventListener("click", () => loadAppLogs(true));
  refs.appLogsDownload.addEventListener("click", downloadAppLogs);
  refs.screenShell.addEventListener("pointerdown", onPointerDown);
  refs.screenShell.addEventListener("pointermove", onPointerMove);
  refs.screenShell.addEventListener("pointerup", onPointerUp);
  refs.screenShell.addEventListener("pointercancel", onPointerCancel);
  refs.screenShell.addEventListener("lostpointercapture", onPointerCancel);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") cancelContinuousPointers(); });
  window.addEventListener("beforeunload", () => { stopPolling(); stopBackgroundPolling(); });
}

bindEvents();
refs.clientLabel.textContent = compactId(state.clientId);
async function initialize() {
  await loadDevices();
  await Promise.all([loadRecordings(), loadJobs(), loadSessions()]);
  startBackgroundPolling();
  const previous = params.get("session") || sessionStorage.getItem("reproof-live-session");
  if (previous) {
    try {
      const { session } = await apiRequest(sessionPath(previous));
      state.selectedDeviceId = session.deviceId;
      adoptSession(session);
      renderDevices();
      if (session.recordingId) await refreshRecording();
      if (session.state !== "closed") startPolling();
    } catch { sessionStorage.removeItem("reproof-live-session"); }
  }
}
initialize();
renderSessionSurface();
syncControls();
