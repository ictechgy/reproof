import test from "node:test";
import assert from "node:assert/strict";
import { ReleaseClient, assertionFromFields, repairPresentation, TicketReferenceState, refreshIssueView, pollIssueWorkspace } from "./issue.js";

const fields = () => ({ observation: "screen", property: "status", operator: "equals", valueType: "text", value: "error",
  coverage: "snapshot", start: "0", end: "1000", window: "1000", uncertainty: "50", age: "60000", scope: "root", sampling: "250", stability: "0" });
test("default browser fetch keeps its Window receiver", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = function () {
    assert.equal(this, globalThis);
    return Promise.resolve(Response.json({ projects: [] }));
  };
  try { assert.deepEqual(await new ReleaseClient().request("/api/release/projects"), { projects: [] }); }
  finally { globalThis.fetch = previous; }
});
test("condition editor keeps authored timing and typed values explicit", () => {
  const assertion = assertionFromFields("defect", fields());
  assert.deepEqual(assertion.coverage.windowMs, { start: 1000, end: 1000 });
  assert.equal(assertion.stabilityMs, 0);
  assert.equal(assertion.predicate.value, "error");
  assert.equal(assertionFromFields("expected", { ...fields(), valueType: "boolean", value: "false" }).predicate.value, false);
  assert.equal(assertionFromFields("expected", { ...fields(), valueType: "number", value: "42" }).predicate.value, 42);
  assert.equal("value" in assertionFromFields("expected", { ...fields(), operator: "absent" }).predicate, false);
});
test("invalid timing, numeric values and unbound observations are rejected", () => {
  for (const edit of [{ window: "999" }, { observation: "" }, { valueType: "number", value: "" },
    { valueType: "number", value: "Infinity" }, { valueType: "boolean", value: "yes" },
    { coverage: "sampled", sampling: "1" }, { coverage: "continuous", start: "1100" }]) {
    assert.throws(() => assertionFromFields("defect", { ...fields(), ...edit }));
  }
});
test("browser client confines credentials to login and sends CSRF on mutations", async () => {
  const calls = [];
  const api = new ReleaseClient({ fetcher: async (path, options) => {
    calls.push({ path, options });
    return Response.json(path.endsWith("auth/session") ? { csrfToken: "owned-test-csrf", principalId: "qa" } : {});
  } });
  await api.login("owned-test-credential");
  await api.request("/api/release/projects");
  await api.post("/api/release/issues", { projectId: "project" });
  assert.equal(calls[0].options.headers.Authorization, "Bearer owned-test-credential");
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.equal(calls[1].options.headers["X-Repro-CSRF"], undefined);
  assert.equal(calls[2].options.headers.Authorization, undefined);
  assert.equal(calls[2].options.headers["X-Repro-CSRF"], "owned-test-csrf");
  assert.equal(calls.every((call) => call.options.credentials === "same-origin" && call.options.redirect === "error"), true);
  assert.equal(calls.some((call) => call.path.includes("credential") || call.path.includes("csrf")), false);
});

const repairView = () => ({ issue: { id: "issue", projectId: "checkout", state: "reproduced" },
  approval: { specificationDigest: "approved" }, specificationDigest: "approved",
  campaign: { verdict: "reproduced" } });
const repairProject = () => ({ id: "checkout", capabilities: ["project.maintain"],
  repair: { proposalAvailable: true, verificationAvailable: false, providerKind: "local-test-adapter" } });
test("repair requires the exact reproduced revision and maintainer access", () => {
  const state = { project: repairProject(), view: repairView(), jobs: [], principal: "owner" };
  assert.equal(repairPresentation(state).canPropose, true);
  assert.equal(repairPresentation({ ...state, dirty: true }).canPropose, false);
  assert.equal(repairPresentation({ ...state, project: { ...repairProject(), capabilities: [] } }).canPropose, false);
  assert.equal(repairPresentation({ ...state, view: { ...repairView(), specificationDigest: "new" } }).canPropose, false);
  assert.equal(repairPresentation({ ...state, view: { ...repairView(), campaign: { verdict: "unknown" } } }).canPropose, false);
});
test("proposal status never turns provider text into verification or exposes another issue", () => {
  const job = { id: "repair", issueId: "issue", projectId: "checkout", ownerId: "owner", status: "proposal-ready" };
  const state = { project: repairProject(), view: repairView(), jobs: [job], principal: "owner" };
  assert.match(repairPresentation(state).label, /verification pending/);
  assert.equal(repairPresentation(state).canReview, true);
  assert.equal(repairPresentation({ ...state, jobs: [{ ...job, outputsExpired: true }] }).canReview, false);
  assert.equal(repairPresentation({ ...state, jobs: [{ ...job, status: "verified", result: { verified: true } }] }).canReview, false);
  assert.equal(repairPresentation({ ...state, jobs: [{ ...job, issueId: "other" }] }).job, null);
});
test("active repair blocks duplicate generation and retains cancellation", () => {
  const job = { id: "repair", issueId: "issue", projectId: "checkout", ownerId: "owner", status: "running" };
  const state = { project: repairProject(), view: repairView(), jobs: [job], principal: "owner" };
  assert.equal(repairPresentation(state).canPropose, false);
  assert.equal(repairPresentation(state).canCancel, true);
  assert.equal(repairPresentation({ ...state, jobs: [{ ...job, status: "cancelled" }] }).canCancel, false);
});

test("verification requires the registered environment and all execution permissions", () => {
  const project = { ...repairProject(), capabilities: ["project.maintain", "replay.execute", "fixture.execute"],
    repair: { ...repairProject().repair, verificationAvailable: true } };
  const state = { project, view: repairView(), principal: "owner" };
  assert.equal(repairPresentation(state).canVerify, true);
  for (const capability of project.capabilities) {
    assert.equal(repairPresentation({ ...state, project: { ...project,
      capabilities: project.capabilities.filter((item) => item !== capability) } }).canVerify, false);
  }
  assert.equal(repairPresentation({ ...state, dirty: true }).canVerify, false);
  assert.equal(repairPresentation({ ...state, project: { ...project,
    repair: { ...project.repair, verificationAvailable: false } } }).canVerify, false);
});

test("verified presentation requires matching supervisor evidence and complete repetitions", () => {
  const job = { id: "repair", issueId: "issue", projectId: "checkout", status: "verified",
    plan: { digest: "plan", specificationDigest: "approved", attemptBudget: { candidate: 3 } },
    result: { verified: true, repairPlanDigest: "plan", candidateSourceDigest: "candidate",
      afterEvidence: { cleanupConfirmed: true, repairPlanDigest: "plan", specificationDigest: "approved",
        sourceDigest: "candidate", validation: { status: "pass" }, attempts: [{}, {}, {}] } } };
  const state = { project: repairProject(), view: repairView(), jobs: [job], principal: "owner" };
  assert.equal(repairPresentation(state).verified, true);
  assert.equal(repairPresentation(state).canReview, true);
  assert.match(repairPresentation(state).label, /Verified/);
  for (const change of [{ cleanupConfirmed: false }, { sourceDigest: "other" },
    { specificationDigest: "other" }, { attempts: [{}] }, { validation: { status: "failed" } }]) {
    const changed = { ...job, result: { ...job.result, afterEvidence: { ...job.result.afterEvidence, ...change } } };
    assert.equal(repairPresentation({ ...state, jobs: [changed] }).verified, false);
    assert.equal(repairPresentation({ ...state, jobs: [changed] }).canReview, false);
  }
});


const ticketContext = () => ({ principal: "owner", project: { id: "checkout", projectDigest: "b".repeat(64),
  capabilities: ["specification.maintain", "export.read"] }, view: {
  issue: { id: "issue_a", projectId: "checkout" }, specification: { revision: 1 }, specificationDigest: "a".repeat(64),
  recording: { recordingDigest: "c".repeat(64), original: {} }, approval: null } });
const ticketPackage = () => ({ id: "package_a", projectId: "checkout", projectDigest: "b".repeat(64), expiresAtMs: 2000 });
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
function ticketState(api = { request: async () => ({ references: [], unavailableCount: 0 }) }, options = {}) {
  const state = new TicketReferenceState(api, { now: () => 1000, ...options });
  state.sync(ticketContext()); return state;
}
test("ticket candidate adopts only an explicit current project export in memory", () => {
  const state = ticketState();
  assert.equal(state.candidate, null);
  for (const change of [{ id: "https://tracker/1" }, { projectId: "other" }, { projectDigest: "different" }, { expiresAtMs: 999 }]) {
    assert.throws(() => state.acceptExport({ ...ticketPackage(), ...change }, state.capture()));
  }
  assert.equal(state.acceptExport(ticketPackage(), state.capture()), true);
  assert.deepEqual(state.candidate, { id: "package_a", expiresAtMs: 2000 });
  assert.equal(ticketState().candidate, null);
});
test("ticket link posts only the exact four fields and reuses the selected package", async () => {
  const calls = []; const context = ticketContext(); const before = structuredClone(context);
  const state = ticketState({ post: async (path, body, options) => { calls.push({ path, body, options }); },
    request: async () => ({ references: [{ ticketId: "QA-42", packageId: "package_a", status: "matches-current-revision" }], unavailableCount: 1 }) });
  state.acceptExport(ticketPackage(), state.capture());
  await state.link("QA-42"); await state.link("QA-42");
  assert.equal(calls.length, 2);
  assert.equal(calls.every((call) => call.path === "/api/release/issues/issue_a/ticket-references"), true);
  assert.deepEqual(calls[0].body, { ticketId: "QA-42", packageId: "package_a", baseRevision: 1, baseSpecificationDigest: "a".repeat(64) });
  assert.deepEqual(calls[0].body, calls[1].body); assert.equal(calls[0].options.signal instanceof AbortSignal, true);
  assert.deepEqual(context, before); assert.equal(state.unavailableCount, 1); assert.equal(state.references.length, 1);
});
test("ticket validation never exports, approves or contacts a tracker", () => {
  const state = ticketState();
  assert.throws(() => state.link("QA-42"), /Export/);
  state.acceptExport(ticketPackage(), state.capture());
  for (const ticket of ["", "https://tracker/1", "QA 42", "_QA", "a".repeat(65), "private\ntext"]) assert.throws(() => state.link(ticket));
});
test("repeated ticket clicks are serialized until the actual request settles", async () => {
  const gate = deferred(); let posts = 0;
  const state = ticketState({ post: async () => { posts += 1; await gate.promise; },
    request: async () => ({ references: [], unavailableCount: 0 }) });
  state.acceptExport(ticketPackage(), state.capture());
  const first = state.link("QA-42"); await state.link("QA-42"); await state.refresh();
  assert.equal(posts, 1); assert.equal(state.busy, true);
  gate.resolve(); await first; assert.equal(state.busy, false);
});
test("navigation, principal, capability and revision changes discard ticket state", () => {
  const changes = [c => { c.view.issue.id = "issue_b"; }, c => { c.project.id = "other"; }, c => { c.principal = "other"; },
    c => { c.project.capabilities.pop(); }, c => { c.project.capabilities.push("replay.execute"); },
    c => { c.project.projectDigest = "d".repeat(64); }, c => { c.view.specification.revision = 2; },
    c => { c.view.specificationDigest = "e".repeat(64); }, c => { c.view.recording.recordingDigest = "f".repeat(64); }];
  for (const change of changes) {
    const state = ticketState(); const token = state.capture(); state.acceptExport(ticketPackage(), token);
    state.references = [{ ticketId: "QA-42" }]; const context = ticketContext(); change(context); state.sync(context);
    assert.equal(state.candidate, null); assert.deepEqual(state.references, []);
    assert.equal(state.acceptExport(ticketPackage(), token), false);
  }
});
test("late ticket read cannot resurrect a prior issue or reset a newer pending operation", async () => {
  const old = deferred(); const newer = deferred(); let reads = 0;
  const state = ticketState({ request: () => (++reads === 1 ? old.promise : newer.promise) });
  const first = state.refresh(); state.reset(); state.sync(ticketContext()); const second = state.refresh();
  old.resolve({ references: [{ ticketId: "OLD" }], unavailableCount: 0 }); await first;
  assert.deepEqual(state.references, []); assert.equal(state.busy, true);
  newer.resolve({ references: [{ ticketId: "NEW" }], unavailableCount: 0 }); await second;
  assert.equal(state.references[0].ticketId, "NEW"); assert.equal(state.busy, false);
});
test("late ticket link after logout cannot issue a follow-up read or restore metadata", async () => {
  const gate = deferred(); let reads = 0; let signal;
  const state = ticketState({ post: async (_path, _body, options) => { signal = options.signal; await gate.promise; }, request: async () => { reads++; } });
  state.acceptExport(ticketPackage(), state.capture()); const linking = state.link("QA-42"); state.reset();
  assert.equal(signal.aborted, true); gate.resolve(); await linking;
  assert.equal(reads, 0); assert.equal(state.candidate, null); assert.deepEqual(state.references, []);
});
test("candidate expiry at async completion clears metadata without waiting for a timer", async () => {
  let now = 1000; const gate = deferred();
  const state = ticketState({ request: () => gate.promise }, { now: () => now });
  state.acceptExport(ticketPackage(), state.capture()); const reading = state.refresh(); now = 2000;
  gate.resolve({ references: [{ ticketId: "EXPIRED" }], unavailableCount: 0 }); await reading;
  assert.equal(state.candidate, null); assert.deepEqual(state.references, []); assert.equal(state.busy, false); assert.match(state.message, /expired/);
});
test("failed and uncertain ticket requests clear stale metadata; old failures cannot clear a new scope", async () => {
  for (const status of [401, 403, 404, 409, 410, 500]) {
    const state = ticketState({ request: async () => { throw Object.assign(new Error("unavailable"), { status }); } });
    state.acceptExport(ticketPackage(), state.capture()); state.references = [{ ticketId: "OLD" }];
    await assert.rejects(state.refresh(), /unavailable/); assert.equal(state.candidate, null); assert.deepEqual(state.references, []);
  }
  const gate = deferred(); const state = ticketState({ request: () => gate.promise });
  const pending = state.refresh(); state.reset(); state.sync(ticketContext()); state.acceptExport(ticketPackage(), state.capture());
  gate.reject(new Error("old failure")); await pending; assert.equal(state.candidate.id, "package_a");
});
test("refresh recovery replaces the list without recovering an invalidated package candidate", async () => {
  let fail = true;
  const state = ticketState({ request: async () => { if (fail) throw new Error("offline"); return { references: [{ ticketId: "QA-42" }], unavailableCount: 2 }; } });
  state.acceptExport(ticketPackage(), state.capture()); await assert.rejects(state.refresh()); fail = false; await state.refresh();
  assert.equal(state.candidate, null); assert.equal(state.references[0].ticketId, "QA-42"); assert.equal(state.unavailableCount, 2);
});


test("retention omissions clear an unavailable candidate but retain one still listed", async () => {
  let result = { references: [{ packageId: "package_a" }], unavailableCount: 1 };
  const state = ticketState({ request: async () => result }); state.acceptExport(ticketPackage(), state.capture());
  await state.refresh(); assert.equal(state.candidate.id, "package_a");
  result = { references: [], unavailableCount: 2 }; await state.refresh();
  assert.equal(state.candidate, null); assert.deepEqual(state.references, []); assert.equal(state.busy, false);
});
test("uncertain link failure clears metadata and explicit refresh discovers only the completed reference", async () => {
  const state = ticketState({ post: async () => { throw new TypeError("connection lost after commit"); },
    request: async () => ({ references: [{ ticketId: "QA-42", packageId: "package_a" }], unavailableCount: 0 }) });
  state.acceptExport(ticketPackage(), state.capture()); state.references = [{ ticketId: "OLD" }];
  await assert.rejects(state.link("QA-42"), /connection lost/);
  assert.equal(state.candidate, null); assert.deepEqual(state.references, []); assert.equal(state.busy, false);
  await state.refresh(); assert.equal(state.references[0].ticketId, "QA-42"); assert.equal(state.candidate, null);
});
test("a late failed link cannot erase the next issue's candidate", async () => {
  const gate = deferred(); const state = ticketState({ post: () => gate.promise });
  state.acceptExport(ticketPackage(), state.capture()); const pending = state.link("QA-42");
  state.reset(); state.sync(ticketContext()); state.acceptExport({ ...ticketPackage(), id: "package_b" }, state.capture());
  gate.reject(new TypeError("old connection failed")); await pending; assert.equal(state.candidate.id, "package_b");
});


test("sign-out clears the active CSRF but retains a serialized retry until confirmed", async () => {
  const gate = deferred(); const calls = []; let fail = true;
  const api = new ReleaseClient({ csrf: "owned-csrf", fetcher: async (path, options) => {
    calls.push({ path, options });
    if (fail) { await gate.promise; throw new TypeError("owned network failure"); }
    return new Response(null, { status: 204 });
  } });
  const pending = api.logout(); assert.equal(api.csrf, null); assert.equal(api.logout(), pending); assert.equal(calls.length, 1);
  gate.resolve(); await assert.rejects(pending, /network failure/); fail = false;
  await api.logout(); assert.equal(calls.length, 2);
  assert.equal(calls.every(({ path, options }) => path === "/api/auth/logout" && options.headers["X-Repro-CSRF"] === "owned-csrf" && options.credentials === "same-origin"), true);
  assert.equal(api.logoutCsrf, null); assert.equal(api.logoutPromise, null);
});

// Exercise the same refresh chain and polling callback used by the workspace,
// including failures before the ticket endpoint is reached (no DOM or browser).
function pollingFixture() {
  const state = { ...ticketContext(), generation: 1, readSequence: 0, polling: false, busy: false,
    scope: new AbortController(), lastCatalogAt: performance.now() };
  state.view.issue.state = "complete";
  const calls = [], renders = [], notices = [];
  let failure = null, failurePath = "/issue", resets = 0;
  const reference = { ticketId: "QA-42", packageId: "package_a", status: "matches-current-revision" };
  const api = { csrf: "test", request: async (path) => {
    calls.push(path);
    if (path === failurePath && failure) return await failure();
    if (path.endsWith("ticket-references")) return { references: [], unavailableCount: 1 };
    return structuredClone(state.view);
  } };
  const renderTickets = () => renders.push({ references: structuredClone(tickets.references), loaded: tickets.loaded,
    message: tickets.message, candidate: tickets.candidate });
  const tickets = new TicketReferenceState(api, { now: () => 1000, changed: renderTickets });
  tickets.sync(state); tickets.references = [reference]; tickets.loaded = true;
  const refreshCatalog = async () => {};
  const refreshView = () => refreshIssueView({ state, api, issuePath: () => "/issue", renderView: () => tickets.sync(state),
    refreshRepairs: () => api.request("/repairs"), tickets, refreshCatalog });
  const poll = (overrides = {}) => pollIssueWorkspace({ state, api, tickets, syncTickets: () => tickets.sync(state), renderTickets,
    refreshView, refreshCatalog, liveFrame: async () => {}, resetScope: () => { resets++; tickets.reset(); },
    notice: (message) => notices.push(message), hidden: () => false, ...overrides });
  return { state, api, tickets, calls, renders, notices, poll, reference, resets: () => resets,
    fail: (path, operation) => { failurePath = path; failure = operation; }, recover: () => { failure = null; } };
}
for (const path of ["/issue", "/repairs"]) {
  for (const status of [null, 503]) {
    test(`poll clears retained references without an export after ${path} ${status || "network"} failure and recovers`, async () => {
      const f = pollingFixture();
      assert.equal(f.tickets.candidate, null); // Ordinary navigation, not the export/link flow.
      f.fail(path, () => { throw status ? Object.assign(new Error("unavailable"), { status }) : new TypeError("offline"); });
      for (let attempt = 0; attempt < 3; attempt++) {
        await f.poll();
        assert.deepEqual(f.tickets.references, []);
        assert.equal(f.tickets.loaded, false);
        assert.equal(f.tickets.candidate, null);
        assert.match(f.renders.at(-1).message, /References unavailable/);
        assert.equal(f.renders.at(-1).loaded, false);
        assert.deepEqual(f.renders.at(-1).references, []);
        assert.equal(f.state.polling, false);
      }
      assert.equal(f.calls.some((call) => call.endsWith("ticket-references")), false);
      assert.equal(f.resets(), 0); assert.equal(f.notices.length, 3);
      f.recover(); await f.poll();
      assert.equal(f.tickets.loaded, true); assert.equal(f.tickets.unavailableCount, 1);
      assert.deepEqual(f.tickets.references, []); assert.equal(f.tickets.message, "");
      assert.equal(f.calls.filter((call) => call.endsWith("ticket-references")).length, 1);
    });
  }
}
test("poll failure also invalidates an exported candidate and any pending reference response", async () => {
  const f = pollingFixture(), pending = deferred();
  f.tickets.acceptExport(ticketPackage(), f.tickets.capture());
  f.fail("/api/release/issues/issue_a/ticket-references", () => pending.promise);
  const read = f.tickets.refresh();
  f.fail("/issue", () => { throw new TypeError("offline"); });
  await f.poll();
  assert.equal(f.tickets.candidate, null); assert.equal(f.tickets.loaded, false);
  pending.resolve({ references: [f.reference], unavailableCount: 0 }); await read;
  assert.deepEqual(f.tickets.references, []); assert.equal(f.tickets.loaded, false);
});
for (const path of ["/issue", "/repairs"]) {
  test(`late ${path} failure from the previous navigation cannot erase current references`, async () => {
    const f = pollingFixture(), pending = deferred(), entered = deferred();
    f.fail(path, () => { entered.resolve(); return pending.promise; });
    const poll = f.poll(); await entered.promise;
    f.state.generation++; f.state.scope.abort(); f.state.scope = new AbortController();
    f.state.view.issue.id = "issue_b"; f.tickets.sync(f.state);
    f.tickets.references = [{ ...f.reference, ticketId: "QA-NEW" }]; f.tickets.loaded = true;
    pending.reject(new TypeError("old offline")); await poll;
    assert.equal(f.tickets.references[0].ticketId, "QA-NEW"); assert.equal(f.tickets.loaded, true);
    assert.deepEqual(f.notices, []); assert.equal(f.resets(), 0); assert.equal(f.state.polling, false);
  });
}
test("aborted poll does not erase references or show a failure", async () => {
  const f = pollingFixture(); f.fail("/issue", () => { throw new DOMException("cancelled", "AbortError"); });
  await f.poll(); assert.deepEqual(f.tickets.references, [f.reference]);
  assert.equal(f.tickets.loaded, true); assert.deepEqual(f.notices, []); assert.equal(f.state.polling, false);
});
for (const status of [401, 403, 404, 410]) {
  test(`poll preserves scope reset on ${status}`, async () => {
    const f = pollingFixture(); f.fail("/issue", () => { throw Object.assign(new Error("denied"), { status }); });
    await f.poll(); assert.equal(f.resets(), 1); assert.deepEqual(f.tickets.references, []);
    assert.equal(f.tickets.context, null); assert.equal(f.state.polling, false);
  });
}
test("poll retains hidden, busy, in-flight and signed-out guards", async () => {
  for (const guard of ["hidden", "busy", "polling", "signed-out"]) {
    const f = pollingFixture();
    if (guard === "signed-out") f.api.csrf = null;
    else if (guard !== "hidden") f.state[guard] = true;
    await f.poll({ hidden: () => guard === "hidden" });
    assert.deepEqual(f.calls, []); assert.deepEqual(f.tickets.references, [f.reference]);
  }
});
for (const supersededBy of ["navigation", "newer read"]) {
  test(`late successful issue read cannot replace ${supersededBy}`, async () => {
    const f = pollingFixture(), pending = deferred();
    const old = structuredClone(f.state.view);
    f.fail("/issue", () => pending.promise);
    const poll = f.poll();
    if (supersededBy === "navigation") f.state.generation++;
    else f.state.readSequence++;
    f.state.view = { ...structuredClone(old), specificationDigest: "newer" };
    f.tickets.sync(f.state); f.tickets.references = [{ ...f.reference, ticketId: "QA-NEW" }]; f.tickets.loaded = true;
    pending.resolve(old); await poll;
    assert.equal(f.state.view.specificationDigest, "newer");
    assert.equal(f.tickets.references[0].ticketId, "QA-NEW");
    assert.equal(f.calls.includes("/repairs"), false);
    assert.equal(f.calls.some((path) => path.endsWith("ticket-references")), false);
  });
}
