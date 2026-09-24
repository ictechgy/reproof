import { SegmentPlayer, actionTime, timelineEnd } from "./video.js";

const ROOT = "/api/release";
const ACTIVE = new Set(["preparing", "recording", "finalizing", "replaying", "cancelling"]);
const clone = (value) => structuredClone(value);
const short = (value) => value ? `${value.slice(0, 12)}…` : "—";
const ms = (value) => Number.isFinite(value) ? `${Math.round(value)} ms` : "Unknown";
const identifier = (value) => /^[a-zA-Z0-9_-]{1,128}$/.test(value || "");
const REPAIR_ACTIVE = new Set(["created", "running"]);

export function repairPresentation({ project, view, jobs = [], principal, dirty = false, busy = false, selectedId = null }) {
  const scoped = jobs.filter((job) => job.issueId === view?.issue.id && job.projectId === project?.id);
  const job = scoped.find((item) => item.id === selectedId) || scoped.at(-1) || null;
  const active = scoped.findLast((item) => REPAIR_ACTIVE.has(item.status)) || null;
  const maintainer = Boolean(project?.capabilities.includes("project.maintain"));
  const reproduced = view?.issue.state === "reproduced" && view.campaign?.verdict === "reproduced"
    && Boolean(view.approval) && view.approval.specificationDigest === view.specificationDigest;
  const expired = Boolean(job?.outputsExpired || job?.retainUntilMs != null && job.retainUntilMs <= Date.now());
  const after = job?.result?.afterEvidence;
  const verified = Boolean(job?.status === "verified" && job.result?.verified === true && job.plan?.digest
    && job.result.repairPlanDigest === job.plan.digest && after?.repairPlanDigest === job.plan.digest
    && after.specificationDigest === job.plan.specificationDigest && after.sourceDigest === job.result.candidateSourceDigest
    && after.cleanupConfirmed === true && after.validation?.status === "pass"
    && Array.isArray(after.attempts) && Number.isInteger(job.plan.attemptBudget?.candidate)
    && job.plan.attemptBudget.candidate > 0 && after.attempts.length === job.plan.attemptBudget.candidate);
  const phases = { building: "Building candidate…", signing: "Checking signed build…", installing: "Preparing verification device…",
    validating: "Running independent checks…", replaying: "Replaying candidate…", finalizing: "Stopping and cleaning up…" };
  const labels = { created: "Queued", running: phases[job?.phase] || "Generating proposal…", "proposal-ready": "Ready for review · verification pending",
    verified: verified ? "Verified · candidate passed all runs" : "Verification evidence unavailable",
    failed: "Repair failed", blocked: "Repair blocked", cancelled: "Cancelled", interrupted: "Interrupted", quarantined: "Cleanup unresolved" };
  const canPropose = !busy && !dirty && !active && maintainer && reproduced && project?.repair?.proposalAvailable === true;
  return { job, active, jobs: scoped, expired, verified,
    label: expired ? "Repair output expired" : labels[job?.status] || "No proposal available",
    canPropose,
    canVerify: Boolean(canPropose && project?.repair?.verificationAvailable === true
      && project.capabilities.includes("replay.execute") && project.capabilities.includes("fixture.execute")),
    canReview: !busy && maintainer && (job?.status === "proposal-ready" || verified) && !expired,
    canCancel: !busy && Boolean(active && (active.ownerId === principal || maintainer)) };
}

export class ReleaseClient {
  constructor({ fetcher = (...args) => globalThis.fetch(...args), csrf = null } = {}) { this.fetcher = fetcher; this.csrf = csrf; }
  async request(path, { method = "GET", body, headers = {}, signal } = {}) {
    const timeout = AbortSignal.timeout(70000);
    const response = await this.fetcher(path, { method, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", cache: "no-store", redirect: "error",
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(method === "POST" && this.csrf ? { "X-Repro-CSRF": this.csrf } : {}), ...headers } });
    const value = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const error = new Error(value?.error?.message || `Request failed (${response.status})`);
      error.status = response.status; error.code = value?.error?.code; throw error;
    }
    return value;
  }
  post(path, body = {}, options = {}) { return this.request(path, { ...options, method: "POST", body }); }
  async login(credential) {
    const result = await this.post("/api/auth/session", {}, { headers: { Authorization: `Bearer ${credential}` } });
    this.csrf = result.csrfToken; return result;
  }
}

export function assertionFromFields(role, fields) {
  if (!["defect", "expected"].includes(role) || !identifier(fields.observation) || !identifier(fields.property)) {
    throw new Error("Choose an observation and a valid property for each condition");
  }
  const integer = (name, min = 0, max = 60000) => {
    const value = Number(fields[name]);
    if (fields[name] === "" || !Number.isInteger(value) || value < min || value > max) throw new Error(`Invalid ${name} duration`);
    return value;
  };
  const predicate = { kind: "property", observationId: fields.observation, property: fields.property, operator: fields.operator };
  if (!["exists", "absent"].includes(fields.operator)) {
    if (fields.valueType === "number") {
      if (fields.value.trim() === "" || !Number.isFinite(Number(fields.value))) throw new Error("Enter a numeric condition value");
      predicate.value = Number(fields.value);
    } else if (fields.valueType === "boolean") {
      if (!["true", "false"].includes(fields.value)) throw new Error("Boolean values must be true or false");
      predicate.value = fields.value === "true";
    } else predicate.value = fields.valueType === "null" ? null : fields.value;
  }
  const end = integer("end");
  const start = fields.coverage === "snapshot" ? end : integer("start");
  const windowMs = integer("window");
  const stabilityMs = fields.coverage === "snapshot" ? 0 : integer("stability", 0, 5000);
  if (start > end || end > windowMs || stabilityMs > end - start) throw new Error("Condition timing exceeds its observation window");
  const coverage = { class: fields.coverage, windowMs: { start, end },
    maxUncertaintyMs: integer("uncertainty"), maxAgeMs: integer("age"), scope: fields.scope,
    properties: [fields.property] };
  if (fields.coverage === "sampled") coverage.samplingIntervalMs = integer("sampling", 100);
  return { id: `${role}_condition`, role, predicate, coverage, windowMs, stabilityMs };
}

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}
function choices(select, items, selected, placeholder = "None available") {
  select.replaceChildren(...items.map((item) => {
    const option = node("option", item.label ?? item.id); option.value = item.id;
    option.disabled = Boolean(item.disabled); return option;
  }));
  if (!items.length) { const option = node("option", placeholder); option.value = ""; select.append(option); }
  if (items.some((item) => item.id === selected)) select.value = selected;
}
function field(container, label, name, value = "", options = null, type = "text") {
  const wrapper = node("label", undefined, "qa-field"); wrapper.append(node("span", label));
  const input = node(options ? "select" : "input"); input.name = name;
  if (options) choices(input, options.map((item) => typeof item === "string" ? { id: item } : item), String(value));
  else { input.type = type; input.value = String(value); input.maxLength = 512; }
  if (type === "number") { input.min = "0"; input.max = "60000"; input.step = "1"; }
  wrapper.append(input); container.append(wrapper); return input;
}
function checkboxes(container, items, selected) {
  container.replaceChildren();
  for (const item of items) {
    const label = node("label", undefined, "qa-check"); const input = node("input");
    input.type = "checkbox"; input.value = item.id; input.checked = selected.includes(item.id);
    label.append(input, node("span", item.label || item.id)); container.append(label);
  }
  if (!items.length) container.append(node("p", "No preparation is registered.", "qa-muted"));
}
const selectedChecks = (element) => Array.from(element.querySelectorAll("input:checked"), (input) => input.value);

const TEMPLATE = `
  <header class="topbar">
    <a class="brand" href="./"><span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span><span><strong>Reproof</strong><small>QA WORKBENCH</small></span></a>
    <div class="topbar-meta"><span id="qa-identity">Shared coordinator</span><button class="button button-secondary" id="qa-logout" hidden>Sign out</button></div>
  </header>
  <div class="qa-notice" id="qa-notice" role="alert" hidden></div>
  <section class="panel qa-login" id="qa-login-panel" aria-labelledby="qa-login-title">
    <p class="eyebrow">PROJECT ACCESS</p><h1 id="qa-login-title">Sign in to your QA workspace</h1>
    <p>Your administrator provides a personal access credential. Project membership controls the devices and evidence you can use.</p>
    <form id="qa-login-form"><label class="qa-field"><span>Access credential</span><input id="qa-credential" type="password" autocomplete="off" required maxlength="512" /></label><button class="button button-primary">Sign in</button></form>
  </section>
  <main class="qa-workspace" id="qa-workspace" hidden>
    <aside class="panel qa-selection" aria-labelledby="qa-select-title">
      <p class="eyebrow">RECORD A NEW ISSUE</p><h1 id="qa-select-title">Prepare your session</h1>
      <label class="qa-field"><span>Project</span><select id="qa-project"></select></label>
      <label class="qa-field"><span>Application</span><select id="qa-application"></select></label>
      <label class="qa-field"><span>Build</span><select id="qa-build"></select></label>
      <label class="qa-field"><span>Device</span><select id="qa-device"></select></label>
      <p id="qa-device-note" class="qa-muted"></p>
      <fieldset><legend>Starting conditions</legend><div id="qa-preparations"></div>
        <label class="qa-check"><input type="checkbox" id="qa-unprepared" /><span>Record with unknown starting conditions</span></label>
      </fieldset>
      <button class="button button-primary button-wide" id="qa-start">Prepare &amp; record</button>
      <p class="qa-muted">Input opens after preparation completes. Stop closes input first, then finalizes video and cleanup.</p>
      <div class="qa-divider"></div><div class="qa-heading"><h2>Issue library</h2><button class="button button-secondary" id="qa-refresh">Refresh</button></div>
      <label class="qa-field"><span>Import issue package</span><input type="file" id="qa-import" accept="application/zip,.zip" /></label>
      <p class="qa-muted">Imported approvals are provenance. Bind and approve a saved revision locally before replay.</p>
      <div id="qa-library" class="qa-library" aria-live="polite"></div>
    </aside>
    <section class="panel qa-stage" aria-labelledby="qa-issue-title">
      <div class="qa-heading"><div><p class="eyebrow">ORIGINAL RECORDING</p><h2 id="qa-issue-title">Choose an issue or start recording</h2></div><span id="qa-state" class="soft-badge">Idle</span></div>
      <p class="qa-muted" id="qa-state-note">The recording preserves what happened, including missing evidence.</p>
      <div class="qa-media-stage">
        <p id="qa-media-empty">Recorded video will appear here.</p>
        <button id="qa-live-target" class="qa-live-target" aria-label="Tap the live device at a point" hidden><img id="qa-live-image" alt="Current live device frame" /></button>
        <video id="qa-video" preload="metadata" playsinline muted aria-label="Original recording video" hidden></video>
      </div>
      <div class="qa-video-controls"><button class="button button-secondary" id="qa-play" disabled>Play</button><button class="button button-secondary" id="qa-pause" disabled>Pause</button>
        <label class="qa-field qa-scrubber"><span>Recording time</span><input id="qa-seek" type="range" min="0" max="1" value="0" step="1" disabled /></label><output id="qa-position">0 ms</output></div>
      <p class="qa-timing" id="qa-timing" role="status">No video selected.</p>
      <div class="button-row"><button class="button button-record" id="qa-stop" disabled>Stop recording</button><button class="button button-danger-quiet" id="qa-cancel" disabled>Cancel operation</button></div>
      <form id="qa-input-form" hidden>
        <fieldset><legend>Record an action</legend><div class="qa-form-grid">
          <label class="qa-field"><span>Action</span><select id="qa-input-action"><option>tap</option><option>long-press</option><option>swipe</option><option>text</option><option>home</option><option>back</option><option>rotate</option><option>launch</option><option>terminate</option></select></label>
          <label class="qa-field" id="qa-target-kind-field"><span>Target</span><select id="qa-target-kind"><option value="coordinates">Frame coordinates</option><option value="accessibility-id">Accessibility ID</option><option value="resource-id">Resource ID</option></select></label>
          <label class="qa-field" id="qa-locator-field"><span>Target identifier</span><input id="qa-locator" maxlength="512" /></label>
          <label class="qa-field" id="qa-variable-field"><span>Registered text variable</span><select id="qa-variable"></select></label>
          <label class="qa-field" id="qa-x-field"><span>X · 0 to 1</span><input id="qa-x" type="number" value="0.5" min="0" max="1" step="0.01" /></label>
          <label class="qa-field" id="qa-y-field"><span>Y · 0 to 1</span><input id="qa-y" type="number" value="0.5" min="0" max="1" step="0.01" /></label>
          <label class="qa-field" id="qa-x2-field"><span>End X · 0 to 1</span><input id="qa-x2" type="number" value="0.8" min="0" max="1" step="0.01" /></label>
          <label class="qa-field" id="qa-y2-field"><span>End Y · 0 to 1</span><input id="qa-y2" type="number" value="0.5" min="0" max="1" step="0.01" /></label>
          <label class="qa-field" id="qa-duration-field"><span>Duration · ms</span><input id="qa-duration" type="number" value="300" min="1" max="5000" step="1" /></label>
          <label class="qa-field" id="qa-orientation-field"><span>Orientation</span><select id="qa-orientation"><option>portrait</option><option>landscape-left</option><option>landscape-right</option></select></label>
        </div><button class="button button-primary" id="qa-send-input">Send action</button></fieldset>
        <p class="qa-muted">Text uses a registered variable. Its resolved value is kept out of the recording.</p>
      </form>
      <section aria-labelledby="qa-actions-title"><div class="qa-heading"><h3 id="qa-actions-title">Recorded actions</h3><span id="qa-action-count" class="soft-badge">0</span></div><ol id="qa-actions" class="qa-actions"></ol></section>
      <details class="qa-details"><summary>Immutable facts and lifecycle receipts</summary><pre id="qa-facts">No original selected.</pre></details>
      <section id="qa-repair-panel" hidden aria-labelledby="qa-repair-title">
        <div class="qa-divider"></div><h3 id="qa-repair-title">Repair</h3>
        <p id="qa-repair-provider" class="qa-muted"></p>
        <p id="qa-repair-note" class="qa-muted"></p>
        <div class="button-row"><button class="button button-primary" id="qa-propose" disabled>Generate proposal</button><button class="button button-secondary" id="qa-verify" disabled>Generate &amp; verify</button></div>
        <p id="qa-verification-note" class="qa-muted"></p>
        <p id="qa-repair-status" class="qa-revision" role="status" aria-live="polite"></p>
        <div id="qa-repair-list" class="qa-repair-list"></div>
        <button class="button button-danger-quiet" id="qa-repair-cancel" disabled>Cancel repair</button>
        <details id="qa-repair-review" class="qa-details" hidden open><summary>Review changes</summary><pre id="qa-repair-patch"></pre></details>
      </section>
    </section>
    <aside class="panel qa-author" aria-labelledby="qa-author-title">
      <p class="eyebrow">EXECUTABLE SPECIFICATION</p><h2 id="qa-author-title">Define and approve replay</h2>
      <p class="qa-muted">These are authored requirements. Saving them creates a new revision; unknown original conditions stay unknown.</p>
      <p id="qa-editor-empty" class="qa-muted">Freeze a recording to define its actions and conditions.</p>
      <form id="qa-spec-form" hidden>
        <fieldset id="qa-editor-fields"><legend>Selected revision</legend>
          <details class="qa-details" open><summary>Actions, waits and variable bindings</summary><div id="qa-edit-actions"></div></details>
          <fieldset><legend>Replay preparation</legend><div id="qa-edit-fixtures"></div></fieldset>
          <div id="qa-assertions"></div>
          <div class="button-row"><button class="button button-primary" id="qa-save">Save new revision</button><button type="button" class="button button-secondary" id="qa-discard">Reload saved revision</button></div>
        </fieldset>
      </form>
      <section id="qa-approval-panel" hidden>
        <p id="qa-revision" class="qa-revision"></p><details class="qa-details"><summary>Review exact saved specification</summary><pre id="qa-spec-bytes"></pre></details>
        <label id="qa-bind-label" class="qa-check" hidden><input id="qa-bind" type="checkbox" /><span>Bind this imported issue to the selected project and registered preparation</span></label>
        <button class="button button-primary" id="qa-approve">Approve this revision</button>
        <p id="qa-approval-note" class="qa-muted"></p>
        <div class="button-row"><button class="button button-primary" id="qa-replay" disabled>Replay · 3 original attempts</button><button class="button button-secondary" id="qa-export">Export package</button></div>
      </section>
      <section id="qa-results-panel" hidden><h3>Replay results</h3><p class="qa-muted">Injection, observations and reproduction are reported separately. This run does not validate a code repair.</p><div id="qa-results"></div><details class="qa-details"><summary>Predicate and cleanup receipts</summary><pre id="qa-results-detail"></pre></details></section>
    </aside>
  </main>`;

export function mountIssueConsole(root, mode) {
  root.innerHTML = TEMPLATE; root.hidden = false;
  const $ = (name) => root.querySelector(`#qa-${name}`);
  const memory = { get: (key) => { try { return sessionStorage.getItem(key); } catch { return null; } },
    set: (key, value) => { try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch {} } };
  const api = new ReleaseClient({ csrf: memory.get("repro-qa-csrf") });
  const state = { projects: [], issues: [], project: null, view: null, session: null, frame: null,
    principal: memory.get("repro-qa-principal"), client: memory.get("repro-qa-client") || `browser_${crypto.randomUUID().replaceAll("-", "")}`,
    generation: 0, readSequence: 0, catalogSequence: 0, lastCatalogAt: 0,
    scope: new AbortController(), busy: false, polling: false,
    editorKey: null, editorBaseRevision: 0, dirty: false, videoKey: null, imageURL: null, frameId: null, lastHeartbeat: 0, actionEditors: [], assertions: [],
    repairs: [], repairSequence: 0, repairListKey: null, selectedRepairId: null, reviewedRepairId: null };
  memory.set("repro-qa-client", state.client);
  const notice = (message) => { $("notice").textContent = message || ""; $("notice").hidden = !message; };
  const player = new SegmentPlayer($("video"), (point) => {
    $("position").textContent = ms(point.offsetMs); $("seek").value = String(point.offsetMs || 0);
    const empty = point.kind !== "frame";
    $("media-empty").hidden = !empty;
    if (empty) $("media-empty").textContent = point.kind === "gap" ? `Video gap · ${point.reason}`
      : point.kind === "loading" ? "Loading and checking this segment…" : point.reason;
    if (point.kind === "frame") {
      const age = point.sourceAgeMs ? `${ms(point.sourceAgeMs.minimum)}–${ms(point.sourceAgeMs.maximum)}` : "Unknown native acquisition";
      $("timing").textContent = `Source age: ${age} · Timestamp uncertainty: ${ms(point.timingUncertaintyMs)} · Next sample interval: ${ms(point.samplingIntervalMs)} · Decoder seek difference: ${ms(point.seekErrorMs)}`;
    } else $("timing").textContent = point.kind === "gap" ? `No frame is evidence for ${ms(point.offsetMs)}. ${point.reason}` : $("media-empty").textContent;
    $("play").disabled = empty; $("pause").disabled = empty;
  });
  const capability = (name) => state.project?.capabilities.includes(name);
  const issuePath = (suffix = "") => `${ROOT}/issues/${state.view.issue.id}${suffix}`;
  function clearImage() {
    $("live-target").hidden = true; $("live-image").removeAttribute("src");
    if (state.imageURL) URL.revokeObjectURL(state.imageURL);
    state.imageURL = null; state.frameId = null; state.frame = null;
  }
  function resetScope() {
    state.generation += 1; state.readSequence += 1; state.catalogSequence += 1;
    state.scope.abort(); state.scope = new AbortController();
    player.setIssue(null, null); clearImage(); state.session = null; state.view = null;
    state.editorKey = null; state.dirty = false; state.videoKey = null; state.actionEditors = []; state.assertions = [];
    state.repairs = []; state.repairSequence += 1; state.selectedRepairId = null; state.reviewedRepairId = null; state.repairListKey = null;
    $("repair-panel").hidden = true; $("repair-review").hidden = true; $("repair-patch").textContent = ""; $("repair-list").replaceChildren();
    $("issue-title").textContent = "Choose an issue or start recording"; $("state").textContent = "Idle";
    $("spec-form").hidden = true; $("approval-panel").hidden = true; $("results-panel").hidden = true;
    $("results").replaceChildren(); $("results-detail").textContent = "";
    $("editor-empty").hidden = false; $("facts").textContent = "No original selected.";
    $("actions").replaceChildren(); $("action-count").textContent = "0";
    $("seek").disabled = true; $("play").disabled = true; $("pause").disabled = true;
    $("media-empty").hidden = false; $("media-empty").textContent = "Recorded video will appear here.";
    $("timing").textContent = "No video selected."; $("bind").checked = false; controls();
  }
  function controls() {
    const issue = state.view?.issue;
    const owned = issue?.ownerId === state.principal;
    const recording = issue?.state === "recording" && owned;
    const device = state.project?.devices.find((item) => item.id === $("device").value);
    $("start").disabled = state.busy || !capability("session.create") || device?.state !== "available"
      || !$("build").value || Boolean(issue && ACTIVE.has(issue.state));
    $("stop").disabled = state.busy || !recording;
    $("cancel").disabled = state.busy || !issue || !ACTIVE.has(issue.state)
      || (issue.state === "replaying" ? issue.replayOwnerId !== state.principal : !owned);
    $("input-form").hidden = !recording; $("send-input").disabled = state.busy || !state.session;
    $("editor-fields").disabled = state.busy || !capability("specification.maintain") || issue?.state === "replaying";
    $("approve").disabled = state.busy || state.dirty || !capability("specification.maintain") || Boolean(issue && ACTIVE.has(issue.state));
    $("replay").disabled = state.busy || state.dirty || !state.view?.approval || !capability("replay.execute")
      || device?.state !== "available" || Boolean(issue && ACTIVE.has(issue.state));
    $("export").disabled = state.busy || !capability("export.read");
    $("import").disabled = state.busy || !capability("recording.import");
    $("start").textContent = $("unprepared").checked ? "Record · initial state unknown" : "Prepare & record";
    renderRepairs();
  }
  const repairState = () => repairPresentation({ project: state.project, view: state.view, jobs: state.repairs,
    principal: state.principal, dirty: state.dirty, busy: state.busy, selectedId: state.selectedRepairId });
  function renderRepairs() {
    const presentation = repairState();
    $("repair-panel").hidden = !state.view?.recording?.original;
    $("propose").disabled = !presentation.canPropose;
    $("repair-cancel").disabled = !presentation.canCancel;
    $("verify").disabled = !presentation.canVerify;
    $("verification-note").textContent = state.project?.repair?.verificationAvailable
      ? "Generate & verify creates a new candidate, runs the approved checks and repeats the same scenario."
      : "Verification is unavailable for this session. A generated proposal remains unverified.";
    $("repair-provider").textContent = state.project?.repair?.providerKind === "local-test-adapter"
      ? "Configured local patch · test adapter" : state.project?.repair?.providerKind === "external-ai"
        ? "AI proposal · approved project transfer policy" : "No proposal provider is configured.";
    $("repair-note").textContent = !capability("project.maintain") ? "A project maintainer can generate and review source changes."
      : state.dirty ? "Save and approve your edits, then reproduce that revision before requesting a proposal."
        : state.view?.campaign?.verdict !== "reproduced" ? "Reproduce the approved original in all 3 attempts before requesting a proposal."
          : "The proposal uses the approved source and scenario. Review the patch before applying it.";
    const reasons = { ai_transfer_denied: "AI transfer is not approved for this project.", agent_unavailable: "The configured provider is unavailable.",
      original_changed: "Original source or build changed.", baseline_unqualified: "The original reproduction is no longer qualified.",
      protected_verification_unavailable: "Protected verification is not connected.", candidate_mismatch: "The candidate did not clear the issue in every run.",
      regression_failed: "An independent check failed.", signature_invalid: "The signed build did not pass inspection.",
      mobile_quarantined: "Device cleanup could not be confirmed.", build_quarantined: "Build cleanup could not be confirmed.",
      signing_quarantined: "Signing cleanup could not be confirmed." };
    $("repair-status").textContent = presentation.label + (presentation.job?.reason ? ` · ${reasons[presentation.job.reason] || "The request could not complete under the approved policy."}` : "");
    const reviewed = state.repairs.find((job) => job.id === state.reviewedRepairId);
    if (!capability("project.maintain") || !reviewed || reviewed.outputsExpired
      || reviewed.retainUntilMs != null && reviewed.retainUntilMs <= Date.now()) {
      state.reviewedRepairId = null; $("repair-review").hidden = true; $("repair-patch").textContent = "";
    }
    const key = JSON.stringify([presentation.jobs.map((job) => [job.id, job.status, job.outputsExpired,
      job.retainUntilMs != null && job.retainUntilMs <= Date.now(), job.updatedAtMs]),
      capability("project.maintain"), state.busy]);
    if (key === state.repairListKey) return;
    state.repairListKey = key; $("repair-list").replaceChildren();
    for (const job of presentation.jobs) {
      const row = node("div", undefined, "qa-result");
      const display = repairPresentation({ project: state.project, view: state.view, jobs: [job], principal: state.principal, busy: state.busy });
      row.append(node("p", `${short(job.id)} · ${display.label}`));
      if (job.result?.changedPaths) row.append(node("p", `${job.result.changedPaths.length} product file(s) changed`, "qa-muted"));
      const attempts = job.result?.afterEvidence?.attempts || job.attempts?.[0]?.execution?.attempts || [];
      if (display.verified) row.append(node("p", `Original: ${job.plan.attemptBudget.original} reproduced runs · Candidate: ${attempts.length} passed runs`, "qa-muted"));
      for (const attempt of attempts) {
        const status = attempt.status !== "complete" ? attempt.status === "running" ? "In progress" : "Incomplete"
          : attempt.defect === false && attempt.expected === true && attempt.cleanup === "complete" ? "Issue cleared · cleanup complete"
            : attempt.defect === true ? "Defect still observed" : "Result not confirmed";
        row.append(node("p", `Candidate run ${attempt.attempt}: ${status}`, "qa-muted"));
      }
      const button = node("button", "Review proposal", "button button-secondary"); button.disabled = !display.canReview;
      button.addEventListener("click", () => mutation(() => reviewProposal(job.id)));
      row.append(button); $("repair-list").append(row);
    }
    if (state.selectedRepairId === presentation.jobs.at(-1)?.id) $("repair-list").scrollTop = $("repair-list").scrollHeight;
  }
  async function refreshRepairs() {
    if (!state.view || !state.project?.repair?.proposalAvailable) return;
    const generation = state.generation; const sequence = ++state.repairSequence;
    const result = await api.request(issuePath("/repairs"), { signal: state.scope.signal });
    if (generation !== state.generation || sequence !== state.repairSequence) return;
    state.repairs = result.repairs; state.project.repair = result.availability; controls();
  }
  async function reviewProposal(identifier) {
    const generation = state.generation;
    const result = await api.request(`${ROOT}/repairs/${identifier}/proposal`, { signal: state.scope.signal });
    if (generation !== state.generation || !capability("project.maintain")) return;
    const job = state.repairs.find((item) => item.id === identifier);
    if (!job || job.outputsExpired || job.retainUntilMs != null && job.retainUntilMs <= Date.now()) return;
    if (result.verified !== false || result.repairPlanDigest !== job.result?.repairPlanDigest
      || result.candidateSourceDigest !== job.result?.candidateSourceDigest || typeof result.patch !== "string") {
      throw new Error("The returned proposal does not match this repair request");
    }
    state.selectedRepairId = identifier; state.reviewedRepairId = identifier;
    $("repair-patch").textContent = result.patch; $("repair-review").hidden = false; $("repair-review").open = true;
    $("repair-review").scrollIntoView({ block: "nearest" });
  }
  async function mutation(task) {
    if (state.busy) return;
    state.busy = true; state.readSequence += 1; controls(); notice("");
    try { await task(); }
    catch (error) { if (error.name !== "AbortError") notice(error.message); }
    finally { state.busy = false; controls(); }
  }
  function deviceNote() {
    const device = state.project?.devices.find((item) => item.id === $("device").value);
    $("device-note").textContent = device ? `${device.state} · ${device.kind || device.platform}${device.reason ? ` · ${device.reason}` : ""}` : "No authorized device is available.";
    controls();
  }
  function applicationChanged() {
    const project = state.project; if (!project) return;
    const application = $("application").value;
    choices($("build"), project.builds.filter((item) => item.applicationId === application).map((item) => ({ id: item.id })), $("build").value);
    const preparations = project.preparations.filter((item) => item.applicationId === application);
    checkboxes($("preparations"), preparations, preparations.map((item) => item.id));
    $("unprepared").checked = false; deviceNote();
  }
  function chooseProject(id) {
    resetScope(); state.project = state.projects.find((item) => item.id === id) || null;
    if (!state.project) return;
    $("project").value = id;
    choices($("application"), state.project.applications.map((item) => ({ id: item.id, label: item.id })), null);
    choices($("device"), state.project.devices.map((item) => ({ id: item.id, label: `${item.name || item.id} · ${item.state}` })), null);
    choices($("variable"), state.project.variables.filter((item) => ["string", "secret-reference"].includes(item.type)).map((item) => ({ id: item.id, label: `${item.id}${item.secret ? " · secret" : ""}` })), null);
    applicationChanged(); renderLibrary();
  }
  async function refreshCatalog({ initial = false } = {}) {
    const generation = state.generation; const sequence = ++state.catalogSequence;
    const options = { signal: state.scope.signal };
    const [catalog, library] = await Promise.all([api.request(`${ROOT}/projects`, options), api.request(`${ROOT}/issues`, options)]);
    if (generation !== state.generation || sequence !== state.catalogSequence) return;
    state.lastCatalogAt = performance.now();
    state.projects = catalog.projects; state.issues = library.issues;
    const previous = state.project?.id;
    choices($("project"), state.projects, previous, "No authorized project");
    if (initial || !state.projects.some((item) => item.id === previous)) chooseProject($("project").value);
    else {
      state.project = state.projects.find((item) => item.id === previous);
      choices($("device"), state.project.devices.map((item) => ({ id: item.id, label: `${item.name || item.id} · ${item.state}` })), $("device").value);
      deviceNote(); renderLibrary();
    }
  }
  function renderLibrary() {
    const issues = state.issues.filter((item) => item.projectId === state.project?.id).sort((a, b) => b.createdAtMs - a.createdAtMs);
    $("library").replaceChildren();
    for (const issue of issues.slice(0, 200)) {
      const button = node("button", undefined, "qa-library-item"); button.type = "button";
      button.append(node("strong", `${issue.applicationId} · ${short(issue.id)}`), node("span", `${issue.state}${issue.imported ? " · imported" : ""}`));
      button.setAttribute("aria-current", String(state.view?.issue.id === issue.id));
      button.addEventListener("click", () => openIssue(issue.id).catch((error) => notice(error.message)));
      $("library").append(button);
    }
    if (!issues.length) $("library").append(node("p", "No issues in this project yet.", "qa-muted"));
    if (issues.length > 200) $("library").append(node("p", "Showing the 200 most recent issues.", "qa-muted"));
  }
  async function openIssue(id) {
    resetScope(); const generation = state.generation;
    const view = await api.request(`${ROOT}/issues/${id}`, { signal: state.scope.signal });
    if (generation !== state.generation) return;
    state.view = view; renderView(); renderLibrary();
    await refreshRepairs();
    if (view.issue.state === "recording") await liveFrame(generation);
  }
  async function refreshView() {
    if (!state.view) return;
    const generation = state.generation; const sequence = ++state.readSequence;
    const view = await api.request(issuePath(), { signal: state.scope.signal });
    if (generation !== state.generation || sequence !== state.readSequence) return;
    const completed = state.view.issue.state !== view.issue.state && !ACTIVE.has(view.issue.state);
    state.view = view; renderView();
    await refreshRepairs();
    if (completed) await refreshCatalog();
  }
  function renderView() {
    const view = state.view; const issue = view.issue; const original = view.recording?.original;
    const listed = state.issues.findIndex((item) => item.id === issue.id);
    if (listed >= 0 && state.issues[listed].state !== issue.state) { state.issues[listed] = issue; renderLibrary(); }
    $("issue-title").textContent = `${issue.applicationId} · ${short(issue.id)}`; $("state").textContent = issue.state;
    $("state").dataset.state = issue.state;
    $("state-note").textContent = issue.reason || (issue.state === "preparing" ? "Preparing the registered fixture and device. Input is not open yet."
      : issue.state === "finalizing" ? "Input is closed. Finalizing media and confirming cleanup…"
        : original?.unknowns.length ? `${original.unknowns.length} unknown original conditions remain.`
          : view.recording?.status || "Original evidence is still being collected.");
    if (issue.state !== "recording") clearImage();
    $("facts").textContent = JSON.stringify({ recording: view.recording, lifecycle: view.lifecycle }, null, 2);
    const events = original?.events || [];
    $("action-count").textContent = String(events.length); $("actions").replaceChildren();
    for (const event of events.slice(0, 512)) {
      const entry = node("li"); const button = node("button", `${ms(event.offsetMs)} · ${event.input.action} · ${event.dispatch}`, "qa-action");
      button.type = "button"; button.disabled = !view.video;
      button.addEventListener("click", () => player.seek(actionTime(view.video, event)));
      entry.append(button); $("actions").append(entry);
    }
    if (events.length > 512) $("actions").append(node("li", "First 512 actions shown; the original and package retain all actions."));
    const videoKey = view.video ? `${issue.id}:${view.recording.recordingDigest}` : null;
    if (videoKey && state.videoKey !== videoKey) {
      state.videoKey = videoKey; player.setIssue(issue.id, view.video);
      $("seek").max = String(Math.max(1, timelineEnd(view.video, events))); $("seek").disabled = false;
      player.seek(0);
    }
    const editorKey = original ? `${issue.id}:${view.specificationDigest || "draft"}` : null;
    if (editorKey && editorKey !== state.editorKey) {
      if (state.dirty) notice("The saved revision changed. Reload it before continuing your edits.");
      else { state.editorKey = editorKey; renderEditor(view); }
    }
    $("editor-empty").hidden = Boolean(original);
    $("approval-panel").hidden = !view.specification;
    if (view.specification) {
      $("revision").textContent = `Revision ${view.specification.revision} · SHA-256 ${view.specificationDigest}`;
      $("spec-bytes").textContent = view.specificationCanonical;
      $("bind-label").hidden = !issue.imported;
      $("approval-note").textContent = state.dirty ? "Save or reload your edits before approving or replaying a revision."
        : view.approval ? "This exact revision has local approval. The original replay budget is fixed at 3 attempts."
        : "Review the saved revision before approving. Saving edits clears its approval.";
    }
    renderResults(view.campaign); controls();
  }
  function renderResults(campaign) {
    $("results-panel").hidden = !campaign;
    if (!campaign) { $("results").replaceChildren(); $("results-detail").textContent = ""; return; }
    $("results").replaceChildren(node("p", `${campaign.verdict || campaign.state} · ${(campaign.attempts || []).length} recorded attempts`, "qa-revision"));
    for (const [index, attempt] of (campaign.attempts || []).entries()) {
      $("results").append(node("p", `Attempt ${index + 1} · ${attempt.verdict || attempt.status || attempt.state || "Result recorded"}`, "qa-result"));
    }
    $("results-detail").textContent = JSON.stringify(campaign, null, 2);
  }
  function renderAssertion(assertion, role) {
    const section = node("fieldset"); section.append(node("legend", role === "defect" ? "Defect condition" : "Expected condition"));
    const form = node("div", undefined, "qa-form-grid"); const saved = assertion?.predicate?.kind === "property" ? assertion : null;
    if (assertion && !saved) {
      section.append(node("p", "This saved condition combines multiple checks. It will be preserved unchanged.", "qa-muted"), node("pre", JSON.stringify(assertion, null, 2)));
      section._read = () => clone(assertion); $("assertions").append(section); return section;
    }
    const predicate = saved?.predicate; const coverage = saved?.coverage;
    const observations = state.project.observations;
    const observation = field(form, "Observation", "observation", predicate?.observationId, observations);
    field(form, "Property", "property", predicate?.property || "");
    field(form, "Comparison", "operator", predicate?.operator || "equals", ["equals", "not-equals", "exists", "absent", "lt", "lte", "gt", "gte"]);
    field(form, "Value type", "valueType", predicate?.value === null ? "null" : typeof predicate?.value === "number" ? "number" : typeof predicate?.value === "boolean" ? "boolean" : "text", ["text", "number", "boolean", "null"]);
    field(form, "Condition value", "value", predicate?.value === undefined || predicate?.value === null ? "" : String(predicate.value));
    const coverageInput = field(form, "Observation coverage", "coverage", coverage?.class || "snapshot", []);
    const available = () => {
      const kinds = observations.find((item) => item.id === observation.value)?.coverage || [];
      choices(coverageInput, kinds.map((id) => ({ id })), coverageInput.value || coverage?.class, "Unsupported");
    };
    available(); observation.addEventListener("change", available);
    field(form, "Scope", "scope", coverage?.scope || "root", ["root", "window"]);
    for (const [label, name, value] of [
      ["Evaluation window · ms", "window", saved?.windowMs ?? 1000],
      ["Coverage start · ms", "start", coverage?.windowMs.start ?? 1000],
      ["Coverage end / snapshot · ms", "end", coverage?.windowMs.end ?? 1000],
      ["Maximum timestamp uncertainty · ms", "uncertainty", coverage?.maxUncertaintyMs ?? 100],
      ["Maximum evidence age · ms", "age", coverage?.maxAgeMs ?? 60000],
      ["Sample interval · ms", "sampling", coverage?.samplingIntervalMs ?? 250],
      ["Required stability · ms", "stability", saved?.stabilityMs ?? 0],
    ]) field(form, label, name, value, null, "number");
    const sync = () => {
      for (const [name, hidden] of [["start", coverageInput.value === "snapshot"], ["stability", coverageInput.value === "snapshot"], ["sampling", coverageInput.value !== "sampled"]]) {
        form.querySelector(`[name="${name}"]`).parentElement.hidden = hidden;
      }
    };
    coverageInput.addEventListener("change", sync); observation.addEventListener("change", sync); sync();
    section.append(form); $("assertions").append(section);
    section._read = () => assertionFromFields(role, Object.fromEntries(Array.from(form.querySelectorAll("input,select"), (input) => [input.name, input.value])));
    return section;
  }
  function renderEditor(view) {
    const saved = view.specification; const original = view.recording.original;
    state.editorBaseRevision = saved?.revision || 0; state.dirty = false;
    state.editActions = clone(saved?.actions || original.events.map((event) => ({ eventId: event.id, ...event.input })));
    state.editWaits = clone(saved?.waits || []); state.editBindings = clone(saved?.bindings || []);
    state.actionEditors = []; $("edit-actions").replaceChildren();
    for (const action of state.editActions.slice(0, 256)) {
      const entry = node("div", undefined, "qa-edit-action"); entry.append(node("strong", `${action.action} · ${short(action.eventId)}`));
      let target, variable, frameMatch, frameDigest;
      if (action.target) target = field(entry, `${action.target.kind} target`, "target", action.target.value);
      if (action.geometry) {
        const originalGeometry = original.events.find((event) => event.id === action.eventId)?.input.geometry;
        const sameGeometry = originalGeometry && ["width", "height", "rotation", "version"]
          .every((key) => originalGeometry[key] === action.geometry[key]);
        frameDigest = action.geometry.frameDigest || (sameGeometry && originalGeometry.frameDigest);
        frameMatch = field(entry, "Coordinate replay check", "frameMatch", action.geometry.frameDigest ? "frame" : "geometry",
          [...(frameDigest ? [{ id: "frame", label: "Same frame, size and orientation" }] : []),
            { id: "geometry", label: "Same size and orientation" }]);
        entry.append(node("p", `${action.geometry.width} × ${action.geometry.height} · ${action.geometry.rotation}° · The original frame stays unchanged.`, "qa-muted"));
      }
      if (action.action === "text") variable = field(entry, "Variable binding", "variable", action.parameters.variableId,
        state.project.variables.filter((item) => ["string", "secret-reference"].includes(item.type)));
      const wait = field(entry, "Wait after action · ms (0 = none)", "wait",
        state.editWaits.find((item) => item.afterEventId === action.eventId)?.durationMs || 0, null, "number");
      state.actionEditors.push({ action, target, variable, wait, frameMatch, frameDigest }); $("edit-actions").append(entry);
    }
    if (state.editActions.length > 256) $("edit-actions").append(node("p", "The first 256 actions are editable here. Remaining actions and waits are preserved.", "qa-muted"));
    checkboxes($("edit-fixtures"), state.project.preparations.filter((item) => item.applicationId === original.applicationId),
      saved?.fixtures || state.view.issue.preparationIds);
    $("assertions").replaceChildren(); state.assertions = ["defect", "expected"].map((role) => renderAssertion(saved?.assertions.find((item) => item.role === role), role));
    $("spec-form").hidden = false;
  }
  function specificationBody() {
    const actions = clone(state.editActions); const waits = clone(state.editWaits); const bindings = clone(state.editBindings);
    for (const entry of state.actionEditors) {
      const action = actions.find((item) => item.eventId === entry.action.eventId);
      if (entry.target) action.target.value = entry.target.value;
      if (entry.frameMatch) {
        if (entry.frameMatch.value === "geometry") delete action.geometry.frameDigest;
        else if (entry.frameMatch.value === "frame" && entry.frameDigest) action.geometry.frameDigest = entry.frameDigest;
        else throw new Error("Choose a coordinate replay check");
      }
      if (entry.variable) {
        action.parameters.variableId = entry.variable.value;
        if (!bindings.some((item) => item.variableId === entry.variable.value)) bindings.push({ name: entry.variable.value, variableId: entry.variable.value });
      }
      const duration = Number(entry.wait.value);
      if (!Number.isInteger(duration) || (duration !== 0 && (duration < 100 || duration > 60000))) throw new Error("A wait must be 0 or between 100 and 60000 ms");
      const previous = waits.findIndex((item) => item.afterEventId === action.eventId);
      if (previous >= 0) waits.splice(previous, 1);
      if (duration) waits.push({ afterEventId: action.eventId, durationMs: duration });
    }
    return { baseRevision: state.editorBaseRevision, actions, waits, bindings,
      fixtures: selectedChecks($("edit-fixtures")), assertions: state.assertions.map((section) => section._read()) };
  }
  async function liveFrame(generation) {
    const issue = state.view?.issue;
    if (issue?.state !== "recording") return;
    const signal = state.scope.signal;
    const [session, frame] = await Promise.all([
      api.request(`/api/sessions/${issue.sessionId}`, { signal }), api.request(`/api/sessions/${issue.sessionId}/frame`, { signal })]);
    if (generation !== state.generation || state.view?.issue.state !== "recording") return;
    state.session = session.session; controls();
    if (!frame || frame.id === state.frameId) return;
    if (!["image/png", "image/jpeg"].includes(frame.mime) || frame.imageBase64.length > 12 * 1024 * 1024) throw new Error("The live frame format is unavailable");
    const bytes = Uint8Array.from(atob(frame.imageBase64), (value) => value.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: frame.mime }));
    const image = new Image(); image.src = url;
    try {
      await image.decode();
      if (generation !== state.generation || state.view?.issue.state !== "recording") return;
      if (image.naturalWidth !== frame.width || image.naturalHeight !== frame.height) throw new Error("Live frame geometry changed");
      const previous = state.imageURL; state.imageURL = url; state.frame = frame; state.frameId = frame.id;
      $("live-image").src = url; $("live-target").hidden = false; $("media-empty").hidden = true;
      if (previous) URL.revokeObjectURL(previous);
      $("timing").textContent = `Live preview · ${frame.width} × ${frame.height} · Input uses this frame geometry.`;
    } finally { if (state.imageURL !== url) URL.revokeObjectURL(url); }
    if (issue.ownerId === state.principal && Date.now() - state.lastHeartbeat > 15000) {
      state.lastHeartbeat = Date.now();
      await api.post(`/api/sessions/${issue.sessionId}/heartbeat`, { clientId: state.client }, { signal });
    }
  }
  function inputVisibility() {
    const action = $("input-action").value;
    const targetable = ["tap", "long-press", "text"].includes(action);
    if (action === "text" && $("target-kind").value === "coordinates") $("target-kind").value = "accessibility-id";
    const coordinates = action === "swipe" || (targetable && action !== "text" && $("target-kind").value === "coordinates");
    for (const [name, show] of [["target-kind", targetable], ["locator", targetable && !coordinates],
      ["variable", action === "text"], ["x", coordinates], ["y", coordinates], ["x2", action === "swipe"],
      ["y2", action === "swipe"], ["duration", ["long-press", "swipe"].includes(action)], ["orientation", action === "rotate"]]) $(name + "-field").hidden = !show;
  }
  function manualInput() {
    const action = $("input-action").value; const input = { action, parameters: {} };
    if (["tap", "long-press", "text"].includes(action) && $("target-kind").value !== "coordinates") {
      input.target = { kind: $("target-kind").value, value: $("locator").value };
      if (!input.target.value) throw new Error("Enter a target identifier");
    } else if (["tap", "long-press", "swipe"].includes(action)) {
      if (!state.frame) throw new Error("Wait for the current device frame");
      input.geometry = { width: state.frame.width, height: state.frame.height,
        rotation: state.frame.orientation === "portrait" ? 0 : state.frame.orientation === "landscape-right" ? 270 : 90,
        version: state.frame.geometryVersion };
      for (const key of action === "swipe" ? ["x", "y", "x2", "y2"] : ["x", "y"]) {
        const value = Number($(key).value); if (!Number.isFinite(value) || value < 0 || value > 1) throw new Error("Coordinates must be between 0 and 1");
        input.parameters[key] = value;
      }
    }
    if (["long-press", "swipe"].includes(action)) input.parameters.durationMs = Number($("duration").value);
    if (action === "text") input.parameters.variableId = $("variable").value;
    if (action === "rotate") input.parameters.orientation = $("orientation").value;
    if (["launch", "terminate"].includes(action)) input.parameters.applicationId = state.view.issue.applicationId;
    return input;
  }
  async function sendInput() {
    if (!state.session) throw new Error("Wait for the current session");
    const generation = state.generation;
    const result = await api.post(issuePath("/input"), { input: manualInput(),
      operationId: `manual_${crypto.randomUUID().replaceAll("-", "")}`, sequence: state.session.lastSequence + 1,
      controllerId: state.client, epoch: state.session.epoch }, { signal: state.scope.signal });
    if (generation !== state.generation) return;
    state.session = result.session; notice(`Input ${result.receipt.status}.`); await liveFrame(generation);
  }
  async function signedIn() {
    await refreshCatalog({ initial: true });
    $("login-panel").hidden = true; $("workspace").hidden = false; $("logout").hidden = false;
    $("identity").textContent = state.principal || "Project member"; controls();
  }
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault(); let credential = $("credential").value; $("credential").value = "";
    mutation(async () => {
      try {
        const result = await api.login(credential); state.principal = result.principalId;
        memory.set("repro-qa-csrf", api.csrf); memory.set("repro-qa-principal", state.principal);
        await signedIn();
      } finally { credential = ""; }
    });
  });
  $("logout").addEventListener("click", () => mutation(async () => {
    await api.post("/api/auth/logout"); resetScope(); api.csrf = null; state.principal = null;
    memory.set("repro-qa-csrf", null); memory.set("repro-qa-principal", null);
    $("workspace").hidden = true; $("login-panel").hidden = false; $("logout").hidden = true; $("identity").textContent = "Shared coordinator";
  }));
  $("project").addEventListener("change", () => chooseProject($("project").value));
  $("application").addEventListener("change", applicationChanged);
  $("device").addEventListener("change", deviceNote); $("build").addEventListener("change", controls);
  $("unprepared").addEventListener("change", () => { $("preparations").inert = $("unprepared").checked; controls(); });
  $("refresh").addEventListener("click", () => mutation(() => refreshCatalog()));
  $("start").addEventListener("click", () => mutation(async () => {
    const result = await api.post(`${ROOT}/issues`, { projectId: state.project.id, applicationId: $("application").value,
      buildId: $("build").value, deviceId: $("device").value, clientId: state.client,
      preparationIds: $("unprepared").checked ? [] : selectedChecks($("preparations")), unprepared: $("unprepared").checked });
    state.issues.push(result.issue); await openIssue(result.issue.id);
  }));
  for (const action of ["stop", "cancel"]) $(action).addEventListener("click", () => mutation(async () => {
    await api.post(issuePath(`/${action}`), {}, { signal: state.scope.signal }); await refreshView(); await refreshCatalog();
  }));
  $("input-action").addEventListener("change", inputVisibility); $("target-kind").addEventListener("change", inputVisibility); inputVisibility();
  $("input-form").addEventListener("submit", (event) => { event.preventDefault(); mutation(sendInput); });
  $("live-target").addEventListener("click", (event) => {
    if (state.busy || state.view?.issue.ownerId !== state.principal) return;
    const rect = $("live-image").getBoundingClientRect();
    $("input-action").value = "tap"; $("target-kind").value = "coordinates";
    $("x").value = String(event.detail === 0 ? .5 : Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)));
    $("y").value = String(event.detail === 0 ? .5 : Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)));
    inputVisibility(); mutation(sendInput);
  });
  $("spec-form").addEventListener("submit", (event) => { event.preventDefault(); mutation(async () => {
    await api.post(issuePath("/specifications"), specificationBody(), { signal: state.scope.signal }); state.dirty = false; await refreshView();
  }); });
  $("spec-form").addEventListener("input", () => { state.dirty = true; controls(); });
  $("discard").addEventListener("click", () => { state.dirty = false; state.editorKey = null; renderView(); });
  $("approve").addEventListener("click", () => mutation(async () => {
    await api.post(issuePath("/approve"), { specificationDigest: state.view.specificationDigest,
      revision: state.view.specification.revision, bindImported: state.view.issue.imported && $("bind").checked }, { signal: state.scope.signal });
    await refreshView();
  }));
  $("replay").addEventListener("click", () => mutation(async () => {
    await api.post(issuePath("/replay"), { deviceId: $("device").value, clientId: state.client,
      specificationDigest: state.view.specificationDigest }, { signal: state.scope.signal }); await refreshView();
  }));
  const requestRepair = (mode) => mutation(async () => {
    const generation = state.generation;
    const result = await api.post(issuePath("/repairs"), { requestId: `browser_${crypto.randomUUID().replaceAll("-", "")}`,
      specificationDigest: state.view.specificationDigest, mode }, { signal: state.scope.signal });
    if (generation !== state.generation) return;
    state.selectedRepairId = result.repair.id; await refreshRepairs();
  });
  $("propose").addEventListener("click", () => requestRepair("propose"));
  $("verify").addEventListener("click", () => requestRepair("verify"));
  $("repair-cancel").addEventListener("click", () => mutation(async () => {
    const active = repairState().active; if (!active) return;
    await api.post(`${ROOT}/repairs/${active.id}/cancel`, {}, { signal: state.scope.signal }); await refreshRepairs();
  }));
  $("export").addEventListener("click", () => mutation(async () => {
    const generation = state.generation;
    const { package: packaged } = await api.post(issuePath("/export"), {}, { signal: state.scope.signal });
    if (generation !== state.generation) return;
    const link = node("a"); link.href = `${ROOT}/projects/${packaged.projectId}/packages/${packaged.id}/export`;
    link.download = "issue.repro.zip"; link.click();
  }));
  $("import").addEventListener("change", () => {
    const file = $("import").files[0]; $("import").value = ""; if (!file) return;
    mutation(async () => {
      if (file.size < 1 || file.size > 64 * 1024 * 1024) throw new Error("Issue packages must be at most 64 MiB");
      const generation = state.generation; const projectId = state.project.id;
      const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
      if (generation !== state.generation) return;
      const response = await fetch(`${ROOT}/projects/${projectId}/import`, { method: "POST", body: file,
        credentials: "same-origin", redirect: "error", cache: "no-store", signal: AbortSignal.any([state.scope.signal, AbortSignal.timeout(70000)]),
        headers: { "Content-Type": "application/zip", "X-Repro-CSRF": api.csrf,
          "X-Repro-Content-SHA256": Array.from(hash, (byte) => byte.toString(16).padStart(2, "0")).join("") } });
      const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || "Package import failed");
      if (generation !== state.generation) return;
      await refreshCatalog(); await openIssue(result.issue.id);
    });
  });
  $("seek").addEventListener("input", () => player.seek(Number($("seek").value)));
  $("play").addEventListener("click", () => player.play()); $("pause").addEventListener("click", () => player.pause());
  const timer = setInterval(async () => {
    if (!api.csrf || state.polling || state.busy || document.hidden) return;
    state.polling = true; const generation = state.generation;
    try {
      if (state.view) { await refreshView(); if (generation === state.generation) await liveFrame(generation); }
      // Worker availability can arrive after the issue's terminal response.
      // Refresh the catalog even while no issue is selected.
      if (generation === state.generation && performance.now() - state.lastCatalogAt >= 5000) await refreshCatalog();
    }
    catch (error) {
      if (error.name !== "AbortError" && generation === state.generation) {
        if ([401, 403, 404, 410].includes(error.status)) resetScope();
        notice(error.message);
      }
    } finally { state.polling = false; }
  }, 1000);
  const dispose = () => { clearInterval(timer); state.scope.abort(); player.dispose(); clearImage(); };
  window.addEventListener("pagehide", dispose, { once: true });
  document.addEventListener("visibilitychange", () => { if (document.hidden) player.pause(); });
  controls();
  if (!mode.issueWorkflow) { $("login-form").hidden = true; notice("The shared coordinator has no issue runtime configured. An administrator must register the project preparation and observation adapters."); }
  else if (api.csrf) signedIn().catch((error) => { api.csrf = null; memory.set("repro-qa-csrf", null); notice(error.message); });
  return { dispose };
}
