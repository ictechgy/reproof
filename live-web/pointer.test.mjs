import test from "node:test";
import assert from "node:assert/strict";
import { PointerScheduler, MAX_POINTERS } from "./pointer.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("sends down immediately, coalesces moves, and serializes up", async () => {
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => { calls.push(packet); }, now: () => 100 });
  assert.equal(scheduler.pointerDown(42, { sessionId: "s", epoch: 1, x: .1, y: .2 }), true);
  scheduler.pointerMove(42, { sessionId: "s", epoch: 1, x: .2, y: .3 });
  scheduler.pointerMove(42, { sessionId: "s", epoch: 1, x: .4, y: .5 });
  await scheduler.pointerUp(42, { sessionId: "s", epoch: 1, x: .6, y: .7 });
  assert.deepEqual(calls.map(({ phase, x, y, pointerId }) => ({ phase, x, y, pointerId })), [
    { phase: "down", x: .1, y: .2, pointerId: 0 },
    { phase: "move", x: .4, y: .5, pointerId: 0 },
    { phase: "up", x: .6, y: .7, pointerId: 0 },
  ]);
});

test("maps arbitrary browser IDs to bounded slots and reuses after up", async () => {
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => calls.push(packet) });
  for (let browserId = 100; browserId < 100 + MAX_POINTERS; browserId++) {
    assert.equal(scheduler.pointerDown(browserId, { x: 0, y: 0 }), true);
  }
  assert.equal(scheduler.pointerDown(999, { x: 0, y: 0 }), false);
  await scheduler.pointerUp(100, { x: 0, y: 0 });
  assert.equal(scheduler.pointerDown(999, { x: 0, y: 0 }), true);
  assert.equal(calls.find((packet) => packet.pointerId === 0 && packet.phase === "down").pointerId, 0);
});

test("cancellation preserves mandatory edge and clear drops local gestures", async () => {
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => calls.push(packet) });
  scheduler.pointerDown(1, { sessionId: "old", epoch: 4, x: .1, y: .1 });
  scheduler.pointerMove(1, { sessionId: "old", epoch: 4, x: .2, y: .2 });
  await scheduler.cancelAll((gesture) => ({ sessionId: "old", epoch: 4, x: .2, y: .2, pointerId: gesture.slot }));
  assert.deepEqual(calls.map((packet) => packet.phase), ["down", "cancel"]);
  scheduler.clear();
  assert.equal(scheduler.size, 0);
});

test("backpressure keeps one pending move and still delivers up", async () => {
  const first = deferred();
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => {
    calls.push(packet);
    if (packet.phase === "down") await first.promise;
  }});
  scheduler.pointerDown(5, { x: 0, y: 0 });
  for (let i = 1; i <= 100; i++) scheduler.pointerMove(5, { x: i / 100, y: i / 100 });
  const up = scheduler.pointerUp(5, { x: 1, y: 1 });
  assert.equal(scheduler.size, 1);
  first.resolve();
  await up;
  assert.deepEqual(calls.map((packet) => packet.phase), ["down", "move", "up"]);
  assert.deepEqual(calls[1].x, 1);
});

test("one cancel edge clears every active finger", async () => {
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => calls.push(packet) });
  scheduler.pointerDown(10, { x: .1, y: .1 });
  scheduler.pointerDown(11, { x: .2, y: .2 });
  await scheduler.pointerCancel(10, { x: 0, y: 0 });
  assert.equal(scheduler.size, 0);
  assert.deepEqual(calls.map((packet) => packet.phase), ["down", "cancel"]);
});

test("blocked down keeps only one latest move before up", async () => {
  const gate = deferred();
  const calls = [];
  let now = 0;
  const scheduler = new PointerScheduler({ now: () => now, send: async (packet) => {
    calls.push(packet);
    if (packet.phase === "down") await gate.promise;
  }});
  scheduler.pointerDown(7, { x: 0, y: 0 });
  for (let index = 1; index <= 100; index++) {
    now = index * 40;
    scheduler.pointerMove(7, { x: index / 100, y: index / 100 });
  }
  const up = scheduler.pointerUp(7, { x: 1, y: 1 });
  gate.resolve();
  await up;
  assert.deepEqual(calls.map((packet) => packet.phase), ["down", "move", "up"]);
  assert.equal(calls[1].x, 1);
});

test("clear invalidates queued old edges without cancelling the in-flight send", async () => {
  const gate = deferred();
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => {
    calls.push(packet);
    if (packet.phase === "down") await gate.promise;
  }});
  scheduler.pointerDown(1, { epoch: 1, x: 0, y: 0 });
  scheduler.pointerMove(1, { epoch: 1, x: .5, y: .5 });
  const up = scheduler.pointerUp(1, { epoch: 1, x: 1, y: 1 });
  scheduler.clear();
  gate.resolve();
  assert.equal(await up, false);
  await scheduler.drain();
  assert.deepEqual(calls.map((packet) => packet.phase), ["down"]);
});

test("a queued second finger still sends down before an immediate up", async () => {
  const gate = deferred();
  const calls = [];
  const scheduler = new PointerScheduler({ send: async (packet) => {
    calls.push(packet);
    if (packet.pointerId === 0 && packet.phase === "down") await gate.promise;
  }});
  scheduler.pointerDown(10, { x: .1, y: .1 });
  scheduler.pointerDown(11, { x: .2, y: .2 });
  const up = scheduler.pointerUp(11, { x: .2, y: .2 });
  gate.resolve();
  await up;
  assert.deepEqual(calls.map(p => [p.pointerId, p.phase]), [[0, "down"], [1, "down"], [1, "up"]]);
  await scheduler.cancelAll({ x: 0, y: 0 });
});

test("new movement received during an in-flight move is retained", async () => {
  const gate = deferred();
  const entered = deferred();
  const calls = [];
  let now = 100;
  const scheduler = new PointerScheduler({ now: () => now, send: async (packet) => {
    calls.push(packet);
    if (packet.phase === "move" && packet.x === .2) { entered.resolve(); await gate.promise; }
  }});
  scheduler.pointerDown(1, { x: .1, y: .1 });
  await scheduler.drain();
  scheduler.pointerMove(1, { x: .2, y: .2 });
  await entered.promise;
  now = 200;
  scheduler.pointerMove(1, { x: .8, y: .8 });
  gate.resolve();
  await scheduler.drain();
  assert.deepEqual(calls.filter(p => p.phase === "move").map(p => p.x), [.2, .8]);
  await scheduler.pointerUp(1, { x: .8, y: .8 });
});
