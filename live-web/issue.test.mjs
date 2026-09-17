import test from "node:test";
import assert from "node:assert/strict";
import { ReleaseClient, assertionFromFields, repairPresentation } from "./issue.js";

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
