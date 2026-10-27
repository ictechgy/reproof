// Pending plans and operator-entered text exist only in memory, never storage.
export class PlaybackReview {
  constructor({ context, send, changed = () => {}, now = () => Date.now() }) {
    Object.assign(this, { context, send, changed, now });
    this.plan = null; this.busy = false; this.generation = 0; this.error = ""; this.pendingConfirm = null; this.cancelling = false;
  }
  invalidate() { this.generation++; this.plan = null; }
  reconcile() {
    if (this.plan && (this.context().key !== this.plan.key || !this.context().eligible || this.now() >= this.plan.deadline)) this.invalidate();
  }
  async prepare(variables) {
    this.reconcile();
    const c = { ...this.context() };
    if (this.busy || this.cancelling || this.plan || !c.eligible || !c.frame) return;
    const generation = ++this.generation;
    this.busy = true; this.error = ""; this.changed();
    try {
      const result = await this.send(c.id, "/playbacks/plan", {
        controllerId: c.controllerId, epoch: c.epoch, recordingId: c.recordingId,
        variables, frameId: c.frame.id, geometryVersion: c.frame.geometryVersion,
      });
      if (generation !== this.generation || this.context().key !== c.key || !this.context().eligible) return;
      const p = result?.plan;
      if (!p?.token || !Array.isArray(p.actions) || p.verified !== false || p.startingState !== "unknown" || !(p.expiresInSeconds > 0 && p.expiresInSeconds <= 30)) throw new Error("Invalid playback preview; request a new preview.");
      this.plan = { ...p, key: c.key, id: c.id, deadline: this.now() + p.expiresInSeconds * 1000 };
    } catch (error) {
      if (generation === this.generation && this.context().key === c.key) this.error = error.message;
    } finally { this.busy = false; this.changed(); }
  }
  async confirm(acknowledged) {
    this.reconcile();
    if (this.busy || this.cancelling || !this.plan || !acknowledged) return;
    const p = this.plan, c = { ...this.context() };
    if (!c.eligible || !c.frame) return;
    this.plan = null; this.busy = true; this.pendingConfirm = c.id; this.error = "";
    const generation = ++this.generation;
    this.changed();
    try {
      const result = await this.send(c.id, "/playbacks/confirm", {
        controllerId: c.controllerId, epoch: c.epoch, token: p.token,
        acknowledgeUnknownStart: true, frameId: c.frame.id, geometryVersion: c.frame.geometryVersion,
      });
      if (generation === this.generation && this.context().id === c.id && this.context().key === c.key) return result;
    } catch (error) {
      if (generation === this.generation && this.context().id === c.id) this.error = `${error.message} No retry was sent. Check session status before starting a new preview.`;
    } finally { this.pendingConfirm = null; this.busy = false; this.changed(); }
  }
  async cancel() {
    const c = { ...this.context() };
    if (this.cancelling) return;
    if (this.busy && this.pendingConfirm !== c.id) { this.invalidate(); this.changed(); return; }
    if (!this.plan && !c.running && this.pendingConfirm !== c.id) return;
    this.invalidate(); this.cancelling = true; this.error = ""; this.changed();
    try { const result = await this.send(c.id, "/playbacks/cancel", {}); if (this.context().id === c.id && this.context().key === c.key) return result; }
    catch (error) { if (this.context().id === c.id) this.error = `${error.message} Cancellation is uncertain; check session status.`; }
    finally { this.cancelling = false; this.changed(); }
  }
}
