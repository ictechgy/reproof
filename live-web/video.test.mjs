import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { locateTime, timelineEnd, actionTime, fetchVerifiedMedia, LatestMedia, SegmentPlayer } from "./video.js";

function segment(id, start, points, mapped = true) {
  return { segmentId: id, digest: id.repeat(64).slice(0, 64), bytes: 3,
    width: id === "a" ? 96 : 160, height: id === "a" ? 160 : 96,
    displayInterval: { startOffsetMs: start, endOffsetMs: start + points.at(-1) },
    captureInterval: mapped ? {} : null,
    frames: points.map((pts) => ({ presentationTimeMs: pts,
      ...(mapped ? { earliestRecordingOffsetMs: start + pts - 2, latestRecordingOffsetMs: start + pts + 2,
        uncertaintyNs: 2000000 } : { displayOffsetMs: start + pts, uncertaintyNs: null }) })) };
}
function manifest() {
  return { segments: [segment("a", 100, [0, 80, 400]), segment("b", 700, [0, 60, 300])],
    losses: [{ reason: "capture_lost", recordingInterval: { startOffsetMs: 220, endOffsetMs: 260 } }],
    eventMappings: [{ eventId: "event1", recordingOffsetMs: 730, mapping: { kind: "segment" } }] };
}

test("irregular PTS retain source age separately from capture uncertainty and cadence", () => {
  const point = locateTime(manifest(), 210);
  assert.equal(point.presentationTimeMs, 110);
  assert.equal(point.frame.presentationTimeMs, 80);
  assert.deepEqual(point.sourceAgeMs, { minimum: 28, maximum: 32 });
  assert.equal(point.timingUncertaintyMs, 2);
  assert.equal(point.samplingIntervalMs, 320);
});
test("declared gaps and missing intervals never select an old or future frame", () => {
  for (const ms of [0, 220, 260, 501, 699, 1001]) {
    assert.equal(locateTime(manifest(), ms).kind, "gap", String(ms));
  }
  assert.equal(locateTime(manifest(), 220).reason, "capture_lost");
});
test("action seeks cross rotation using segment-local time", () => {
  const m = manifest();
  const point = locateTime(m, actionTime(m, { id: "event1", offsetMs: 800 }));
  assert.equal(point.segment.width, 160);
  assert.equal(point.presentationTimeMs, 30);
  assert.equal(timelineEnd(m, [{ offsetMs: 1100 }]), 1100);
});
test("unmapped native acquisition never gains a source age or uncertainty", () => {
  const point = locateTime({ segments: [segment("a", 100, [0, 100], false)], losses: [] }, 150);
  assert.equal(point.sourceAgeMs, null);
  assert.equal(point.timingUncertaintyMs, null);
  assert.equal(point.kind, "frame");
});
test("verified range reader binds response range, byte count, MIME, ETag and digest", async () => {
  const body = new Uint8Array([1, 2, 3]);
  const digest = Buffer.from(await webcrypto.subtle.digest("SHA-256", body)).toString("hex");
  const headers = { "Content-Type": "video/mp4", "Content-Length": "3", "Content-Range": "bytes 0-2/3", ETag: `"sha256-${digest}"` };
  const fetcher = async (_url, options) => {
    assert.equal(options.headers.Range, "bytes=0-2");
    assert.equal(options.credentials, "same-origin");
    return new Response(body, { status: 206, headers });
  };
  assert.deepEqual(await fetchVerifiedMedia("/media", { digest, bytes: 3 }, new AbortController().signal,
    { fetcher, subtle: webcrypto.subtle }), body);
  for (const changed of [{ "Content-Range": "bytes 1-3/3" }, { ETag: '"wrong"' }, { "Content-Type": "text/html" }]) {
    await assert.rejects(fetchVerifiedMedia("/media", { digest, bytes: 3 }, new AbortController().signal,
      { subtle: webcrypto.subtle, fetcher: async () => new Response(body, { status: 206, headers: { ...headers, ...changed } }) }));
  }
});
test("media is denied for cross-origin paths, oversized bodies, checksum mismatch and revoked access", async () => {
  const signal = new AbortController().signal;
  for (const [path, reference, fetcher] of [
    ["https://example.invalid/media", { digest: "a".repeat(64), bytes: 3 }],
    ["//example.invalid/media", { digest: "a".repeat(64), bytes: 3 }],
    ["/media", { digest: "a".repeat(64), bytes: 33 * 1024 * 1024 }],
    ["/media", { digest: "a".repeat(64), bytes: 3 }, async () => new Response(null, { status: 403 })],
    ["/media", { digest: "a".repeat(64), bytes: 3 }, async () => new Response(new Uint8Array([1, 2, 3]),
      { status: 206, headers: { "Content-Range": "bytes 0-2/3", "Content-Length": "3", "Content-Type": "video/mp4", ETag: `"sha256-${"a".repeat(64)}"` } })],
  ]) await assert.rejects(fetchVerifiedMedia(path, reference, signal, { fetcher, subtle: webcrypto.subtle }));
});
test("navigation fences late fetches and retains one object URL", async () => {
  const pending = []; const created = []; const revoked = [];
  const media = new LatestMedia({ read: (_path, _ref, signal) => new Promise((resolve) => pending.push({ resolve, signal })),
    createURL: () => { const url = `blob:${created.length}`; created.push(url); return url; }, revokeURL: (url) => revoked.push(url) });
  const first = media.load("/first", {});
  const second = media.load("/second", {});
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve(new Uint8Array([2]));
  assert.equal(await second, "blob:0");
  pending[0].resolve(new Uint8Array([1]));
  assert.equal(await first, null);
  assert.equal(created.length, 1);
  media.clear();
  assert.deepEqual(revoked, ["blob:0"]);
});
class FakeVideo extends EventTarget {
  readyState = 0; currentTime = 0; hidden = true; src = "";
  paused = true; pause() { this.paused = true; } load() {} removeAttribute() { this.src = ""; }
}
test("a gap seek cancels a pending decode and clears the old picture", async () => {
  const video = new FakeVideo(); const statuses = [];
  const player = new SegmentPlayer(video, (point) => statuses.push(point),
    { media: new LatestMedia({ read: async () => new Uint8Array([1]), createURL: () => "blob:test", revokeURL: () => {} }) });
  player.setIssue("issue1", manifest());
  const seek = player.seek(180);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(video.src, "blob:test");
  await player.seek(240);
  video.readyState = 4;
  video.dispatchEvent(new Event("loadeddata"));
  await seek;
  assert.equal(video.hidden, true);
  assert.equal(video.src, "");
  assert.equal(statuses.at(-1).kind, "gap");
  player.dispose();
});
