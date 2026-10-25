import { readFileSync } from "node:fs";
import test from "node:test";
import assert from "node:assert/strict";
import { ReleaseClient, TicketLookupState, readSelectedIssue, pollIssueWorkspace } from "./issue.js";
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const context = () => ({ project: { id: "project_a", projectDigest: "a".repeat(64), capabilities: ["specification.maintain", "export.read"] }, principal: "qa", authenticated: true });
const reference = (edit = {}) => ({ id: "ref_a", projectId: "project_a", ticketId: "QA-42", issueId: "issue_a", packageId: "package_a", baseRevision: 1, status: "matches-current-revision", ...edit });
const result = (references = [reference()], unavailableCount = 0) => ({ references, unavailableCount });
function fixture(response = async () => result()) {
  const calls = [], changes = [];
  const api = new ReleaseClient({ csrf: "csrf-owned", fetcher: async (path, options) => {
    calls.push({ path, ...options }); return Response.json(await response(path, options));
  } });
  const lookup = new TicketLookupState(api, { changed: () => changes.push(lookup.loaded) }); lookup.sync(context());
  return { lookup, calls, api, changes };
}
test("exact lookup uses only authenticated same-origin POST body and preserves multiple references", async () => {
  const refs = [reference(), reference({ id: "ref_b", issueId: "issue_b", status: "stale" })];
  const { lookup, calls } = fixture(async () => result(refs, 2));
  assert.equal(await lookup.search("QA-42"), true);
  assert.deepEqual(lookup.references, refs); assert.equal(lookup.unavailableCount, 2);
  assert.equal(calls.length, 1); assert.equal(calls[0].path, "/api/release/projects/project_a/ticket-lookup");
  assert.equal(calls[0].method, "POST"); assert.equal(calls[0].body, '{"ticketId":"QA-42"}');
  assert.equal(calls[0].credentials, "same-origin"); assert.equal(calls[0].redirect, "error");
  assert.equal(calls[0].headers["X-Repro-CSRF"], "csrf-owned");
});
test("empty and entirely unavailable results are successful snapshots, never auto-selected", async () => {
  for (const unavailable of [0, 64]) {
    const { lookup } = fixture(async () => result([], unavailable));
    assert.equal(await lookup.search("QA-42"), true); assert.equal(lookup.loaded, true);
    assert.equal(lookup.unavailableCount, unavailable); assert.equal(await lookup.resolve("ref_a"), null);
  }
});
test("validation never trims, folds, accepts prose or treats IDs as paths", async () => {
  const { lookup, calls } = fixture(async () => result([]));
  for (const id of ["", " QA-42", "QA-42 ", "QA/42", "../issue", "_QA", "A".repeat(65), "QA:42", "검색"]) {
    assert.equal(await lookup.search(id), false); assert.equal(lookup.loaded, false);
  }
  assert.equal(calls.length, 0);
  for (const id of ["qa-42", "QA-420", "A".repeat(64), "0_x"]) assert.equal(await lookup.search(id), true);
  assert.deepEqual(calls.map(c => JSON.parse(c.body).ticketId), ["qa-42", "QA-420", "A".repeat(64), "0_x"]);
});
for (const change of ["project", "digest", "principal", "permission", "logout", "navigation", "edit"]) {
  test(`late success/failure is discarded after ${change}`, async () => {
    for (const fails of [false, true]) {
      const pending = deferred(), { lookup, calls } = fixture(() => pending.promise);
      const search = lookup.search("QA-42"); const next = context();
      if (change === "project") next.project.id = "project_b";
      if (change === "digest") next.project.projectDigest = "new";
      if (change === "principal") next.principal = "other";
      if (change === "permission") next.project.capabilities = ["export.read"];
      if (change === "logout") next.authenticated = false;
      if (change === "navigation") lookup.reset();
      else if (change === "edit") lookup.clear();
      else lookup.sync(next);
      assert.equal(calls[0].signal.aborted, true);
      if (fails) pending.reject(new Error("late failure")); else pending.resolve(result());
      assert.equal(await search, false); assert.deepEqual(lookup.references, []);
      assert.equal(lookup.loaded, false); assert.equal(lookup.busy, false); assert.equal(lookup.message, "");
    }
  });
}
test("newer repeated search wins even if older success arrives last", async () => {
  const old = deferred(); let count = 0;
  const { lookup } = fixture(() => ++count === 1 ? old.promise : result([reference({ticketId: "QA-NEW"})]));
  const first = lookup.search("QA-42"); await lookup.search("QA-NEW"); old.resolve(result()); await first;
  assert.equal(lookup.ticketId, "QA-NEW"); assert.equal(lookup.references[0].ticketId, "QA-NEW");
});
test("unchanged catalog keeps results, capability changes clear them", async () => {
  const { lookup } = fixture(); await lookup.search("QA-42"); lookup.sync(context()); assert.equal(lookup.loaded, true);
  const next = context(); next.project.capabilities.push("replay.execute"); lookup.sync(next); assert.equal(lookup.loaded, false);
});
test("unauthenticated or insufficient role cannot issue even an empty lookup", async () => {
  for (const edit of [{authenticated: false}, {principal: null}, {project: {...context().project, capabilities: ["export.read"]}}, {project: null}]) {
    const { lookup, calls } = fixture(); lookup.sync({...context(), ...edit});
    assert.equal(await lookup.search("QA-42"), false); assert.equal(calls.length, 0);
  }
});
test("malformed, mismatched and oversized results fail closed without partial disclosure", async () => {
  for (const response of [result([reference({projectId:"project_b"})]), result([reference({ticketId:"qa-42"})]),
    result([reference({issueId:"../escape"})]), result([reference({status:"approved"})]), result([], 65), result([], -1), result([], 0.5), {}, result(Array(65).fill(reference()))]) {
    const { lookup } = fixture(async () => response); assert.equal(await lookup.search("QA-42"), false);
    assert.equal(lookup.loaded, false); assert.deepEqual(lookup.references, []); assert.match(lookup.message, /unavailable/);
  }
});
for (const code of ["ticket_lookup_limit", "ticket_lookup_timeout", "package_expired", "forbidden", "network"]) {
  test(`${code} clears previous results and permits retry`, async () => {
    let fail = false;
    const { lookup } = fixture(async () => { if (fail) throw Object.assign(new Error("private detail"), {code}); return result(); });
    await lookup.search("QA-42"); fail = true; assert.equal(await lookup.search("QA-42"), false);
    assert.equal(lookup.loaded, false); assert.deepEqual(lookup.references, []); assert.equal(lookup.unavailableCount, 0);
    assert.doesNotMatch(lookup.message, /private detail/);
    assert.match(lookup.message, code === "ticket_lookup_limit" ? /64/ : code === "ticket_lookup_timeout" ? /timed out/ : /unavailable/);
    fail = false; assert.equal(await lookup.search("QA-42"), true);
  });
}
test("opening rechecks retention; removed/expired reference never becomes a navigation target", async () => {
  let count = 0; const { lookup, calls } = fixture(async () => ++count === 1 ? result() : result([], 1));
  await lookup.search("QA-42"); assert.equal(await lookup.resolve("ref_a"), null);
  assert.equal(calls.length, 2); assert.match(lookup.message, /no longer available/);
});
test("opening retained stale reference is discovery only and does not execute or export", async () => {
  const { lookup, calls } = fixture(async () => result([reference({status: "stale"})]));
  await lookup.search("QA-42"); assert.equal(await lookup.resolve("ref_a"), "issue_a");
  assert.equal(calls.length, 2); assert.ok(calls.every(call => call.path.endsWith("/ticket-lookup")));
});
test("navigation or logout during selection recheck cannot open previous result", async () => {
  const pending = deferred(); let count = 0;
  const { lookup } = fixture(async () => ++count === 1 ? result() : pending.promise);
  await lookup.search("QA-42"); const selection = lookup.resolve("ref_a"); lookup.reset(); pending.resolve(result());
  assert.equal(await selection, null);
});
test("issue navigation validates project and ID, suppresses stale success/errors, preserves live errors", async () => {
  const view = {issue: {id:"issue_a", projectId:"project_a"}};
  const options = {id:"issue_a", projectId:"project_a", signal:new AbortController().signal, current:()=>true};
  assert.deepEqual(await readSelectedIssue({...options, api:{request:async()=>view}}), view);
  for (const bad of [{issue:{...view.issue,id:"issue_b"}}, {issue:{...view.issue,projectId:"project_b"}}])
    await assert.rejects(readSelectedIssue({...options, api:{request:async()=>bad}}), /selected project/);
  await assert.rejects(readSelectedIssue({...options,id:"../escape",api:{request:async()=>{assert.fail();}}}), /identifier/);
  for (const error of [false,true]) {
    const api = {request:async()=>{if(error) throw new Error("expired"); return view;}};
    assert.equal(await readSelectedIssue({...options,current:()=>false,api}), null);
    if(error) await assert.rejects(readSelectedIssue({...options,api}), /expired/);
  }
});
test("workspace polling failure invalidates lookup and pending responses", async () => {
  const {lookup, api} = fixture(); await lookup.search("QA-42");
  const state = {view:{},generation:0,busy:false,polling:false};
  await pollIssueWorkspace({state,api,lookup,tickets:{clear(){}},syncTickets(){},renderTickets(){},
    refreshView:async()=>{throw new Error("offline");},notice(){},hidden:()=>false});
  assert.equal(lookup.loaded,false); assert.deepEqual(lookup.references,[]); assert.match(lookup.message,/unavailable/);
});

test("ticket input HTML patterns support Unicode Sets and exact bounded IDs", () => {
  const source = readFileSync(new URL("./issue.js", import.meta.url), "utf8");
  const patterns = [...source.matchAll(/pattern="([^"]+)"/g)].map(match => new RegExp(`^(?:${match[1]})$`, "v"));
  assert.ok(patterns.length >= 2);
  for (const pattern of patterns) {
    for (const id of ["QA-42", "qa_42", "A".repeat(64)]) assert.equal(pattern.test(id), true);
    for (const id of [" QA-42", "QA-42 ", "QA/42", "A".repeat(65), ""]) assert.equal(pattern.test(id), false);
  }
});
