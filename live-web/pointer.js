const DEFAULT_MAX_HZ = 30;
const DEFAULT_MAX_POINTERS = 5;

function noop() {}

/** Global bounded pointer transport with one network send in flight. */
export class PointerScheduler {
  constructor({ send, onError = noop, maxHz = DEFAULT_MAX_HZ, maxPointers = DEFAULT_MAX_POINTERS,
    now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    if (typeof send !== "function") throw new TypeError("send is required");
    if (!Number.isFinite(maxHz) || maxHz <= 0 || !Number.isInteger(maxPointers) || maxPointers < 1) {
      throw new TypeError("invalid pointer scheduler bounds");
    }
    this.send = send;
    this.onError = onError;
    this.interval = 1000 / maxHz;
    this.maxPointers = Math.min(maxPointers, DEFAULT_MAX_POINTERS);
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.active = new Map();
    this.edgeQueue = [];
    this.cancelPending = null;
    this.cancelling = false;
    this.cancelWaiters = [];
    this.inflight = null;
    this.generation = 0;
    this.edgeSequence = 0;
  }

  get size() { return this.active.size; }

  _availableSlot() {
    const used = new Set([...this.active.values()].map((gesture) => gesture.slot));
    for (let slot = 0; slot < this.maxPointers; slot += 1) if (!used.has(slot)) return slot;
    return -1;
  }

  _report(error) {
    try { this.onError(error); } catch { /* reporting cannot interrupt transport */ }
  }

  _queueEdge(gesture) {
    if (this.cancelPending || gesture.edgeQueued || gesture.removed || gesture.generation !== this.generation) return;
    gesture.edgeQueued = true;
    gesture.edgeSequence = ++this.edgeSequence;
    this.edgeQueue.push(gesture);
    this._pump();
  }

  pointerDown(browserPointerId, packet) {
    if (this.cancelling || this.active.has(browserPointerId)) return false;
    const slot = this._availableSlot();
    if (slot < 0) return false;
    const gesture = { browserPointerId, slot, generation: this.generation, edgePhase: "down",
      edgeQueued: false, edgeSequence: 0, pendingMove: null, timer: null,
      lastMoveAt: -Infinity, closing: false, removed: false };
    this.active.set(browserPointerId, gesture);
    gesture.downPacket = { ...packet, phase: "down", pointerId: slot };
    this._queueEdge(gesture);
    return true;
  }

  pointerMove(browserPointerId, packet) {
    const gesture = this.active.get(browserPointerId);
    if (!gesture || gesture.closing || gesture.removed) return false;
    gesture.pendingMove = { ...packet, phase: "move", pointerId: gesture.slot };
    const elapsed = this.now() - gesture.lastMoveAt;
    if (elapsed >= this.interval) {
      gesture.moveReady = true;
      this._pump();
    } else if (!gesture.timer) {
      gesture.timer = this.setTimer(() => {
        gesture.timer = null;
        if (!gesture.closing && gesture.pendingMove) {
          gesture.moveReady = true;
          this._pump();
        }
      }, Math.max(0, this.interval - elapsed));
    }
    return true;
  }

  pointerUp(browserPointerId, packet) { return this._finish(browserPointerId, packet, "up"); }

  // The backend cancel edge is CANCELALL; a lost browser pointer cancels all slots.
  pointerCancel(_browserPointerId, packet) { return this.cancelAll(packet); }

  _finish(browserPointerId, packet, phase) {
    const gesture = this.active.get(browserPointerId);
    if (!gesture || gesture.closing || gesture.removed || this.cancelPending) return Promise.resolve(false);
    gesture.closing = true;
    gesture.edgePhase = phase;
    gesture.edgePacket = { ...packet, phase, pointerId: gesture.slot };
    if (gesture.timer) this.clearTimer(gesture.timer);
    gesture.timer = null;
    gesture.moveReady = Boolean(gesture.pendingMove);
    this._queueEdge(gesture);
    return new Promise((resolve) => { gesture.finished = resolve; });
  }

  cancelAll(packetFactory) {
    this.cancelling = true;
    const packet = typeof packetFactory === "function" ? packetFactory({ slot: 0 }) : packetFactory;
    for (const gesture of this.active.values()) {
      gesture.closing = true;
      gesture.pendingMove = null;
      gesture.moveReady = false;
      if (gesture.timer) this.clearTimer(gesture.timer);
      gesture.timer = null;
    }
    if (!this.cancelPending) {
      this.cancelPending = { ...packet, phase: "cancel", pointerId: 0, generation: this.generation };
      this.edgeQueue = [];
    }
    const result = new Promise((resolve) => this.cancelWaiters.push(resolve));
    this._pump();
    return result;
  }

  _nextEdge() {
    while (this.edgeQueue.length) {
      const gesture = this.edgeQueue[0];
      if (gesture.removed || gesture.generation !== this.generation) {
        this.edgeQueue.shift();
        gesture.edgeQueued = false;
        continue;
      }
      if (!gesture.downDispatched) {
        this.edgeQueue.shift();
        gesture.edgeQueued = false;
        gesture.downDispatched = true;
        return { gesture, packet: gesture.downPacket, kind: "down" };
      }
      if (gesture.edgePhase === "up" && gesture.pendingMove) {
        gesture.pendingMove = { ...gesture.pendingMove };
        gesture.moveReady = false;
        gesture.lastMoveAt = this.now();
        return { gesture, packet: gesture.pendingMove, kind: "move" };
      }
      this.edgeQueue.shift();
      gesture.edgeQueued = false;
      const packet = gesture.edgePhase === "down" ? gesture.downPacket : gesture.edgePacket;
      return { gesture, packet, kind: gesture.edgePhase };
    }
    return null;
  }

  _nextMove() {
    let selected = null;
    for (const gesture of this.active.values()) {
      if (!gesture.moveReady || !gesture.pendingMove || gesture.closing || gesture.removed) continue;
      if (!selected || gesture.lastMoveAt < selected.lastMoveAt) selected = gesture;
    }
    if (!selected) return null;
    selected.pendingMove = { ...selected.pendingMove };
    selected.moveReady = false;
    selected.lastMoveAt = this.now();
    return { gesture: selected, packet: selected.pendingMove, kind: "move" };
  }

  _pump() {
    if (this.inflight) return;
    let next = this._nextEdge();
    if (!next && !this.cancelPending) next = this._nextMove();
    if (!next && this.cancelPending && !this.edgeQueue.length) {
      next = { packet: this.cancelPending, kind: "cancel" };
      this.cancelPending = null;
    }
    if (!next) return;
    const generation = this.generation;
    this.inflight = Promise.resolve().then(() => this.send(next.packet)).catch((error) => {
      this._report(error);
    }).then(() => {
      this.inflight = null;
      if (generation !== this.generation) return;
      if (next.kind === "down" && next.gesture?.edgePhase === "up") this._queueEdge(next.gesture);
      if (next.kind === "up" && next.gesture) this._removeGesture(next.gesture);
      if (next.kind === "move" && next.gesture && next.gesture.pendingMove === next.packet) next.gesture.pendingMove = null;
      if (next.kind === "cancel") {
        this.cancelling = false;
        for (const gesture of this.active.values()) this._removeGesture(gesture);
        const waiters = this.cancelWaiters.splice(0);
        waiters.forEach((resolve) => resolve(true));
      }
      this._pump();
    });
  }

  _removeGesture(gesture) {
    gesture.removed = true;
    if (gesture.timer) this.clearTimer(gesture.timer);
    gesture.timer = null;
    if (this.active.get(gesture.browserPointerId) === gesture) this.active.delete(gesture.browserPointerId);
    gesture.finished?.(true);
  }

  clear() {
    this.generation += 1;
    for (const gesture of this.active.values()) {
      gesture.removed = true;
      if (gesture.timer) this.clearTimer(gesture.timer);
      gesture.finished?.(false);
    }
    this.active.clear();
    this.edgeQueue = [];
    this.cancelPending = null;
    this.cancelling = false;
    this.cancelWaiters.splice(0).forEach((resolve) => resolve(false));
  }

  async drain() {
    while (this.inflight) await this.inflight;
  }
}

export const MAX_POINTERS = DEFAULT_MAX_POINTERS;
