// G3 recording time is distinct from segment PTS and source acquisition time.
const MAX_MEDIA_BYTES = 32 * 1024 * 1024;
const RANGE_BYTES = 4 * 1024 * 1024;
const finite = (value) => Number.isFinite(value) && value >= 0;
const abortError = () => new DOMException("Media request cancelled", "AbortError");

export function timelineEnd(manifest, events = []) {
  return Math.max(0, ...(manifest?.segments || []).map((s) => s.displayInterval.endOffsetMs),
    ...(manifest?.losses || []).map((s) => s.recordingInterval.endOffsetMs),
    ...events.map((event) => event.offsetMs || 0));
}

export function actionTime(manifest, event) {
  return manifest?.eventMappings?.find((item) => item.eventId === event.id)?.recordingOffsetMs ?? event.offsetMs;
}

export function locateTime(manifest, offsetMs) {
  if (!finite(offsetMs)) throw new Error("Invalid recording position");
  const gap = (reason) => ({ kind: "gap", offsetMs, reason });
  const loss = manifest?.losses?.find((item) => item.recordingInterval.startOffsetMs <= offsetMs
    && offsetMs <= item.recordingInterval.endOffsetMs);
  if (loss) return gap(loss.reason);
  const segment = manifest?.segments?.findLast((item) => item.displayInterval.startOffsetMs <= offsetMs
    && offsetMs <= item.displayInterval.endOffsetMs);
  if (!segment) return gap("no_recorded_frame");
  const presentationTimeMs = offsetMs - segment.displayInterval.startOffsetMs;
  const index = segment.frames.findLastIndex((frame) => frame.presentationTimeMs <= presentationTimeMs);
  if (index < 0) return gap("no_recorded_frame");
  const frame = segment.frames[index];
  const mapped = segment.captureInterval !== null;
  return { kind: "frame", offsetMs, segment, frame, presentationTimeMs,
    sourceAgeMs: mapped ? { minimum: Math.max(0, offsetMs - frame.latestRecordingOffsetMs),
      maximum: Math.max(0, offsetMs - frame.earliestRecordingOffsetMs) } : null,
    timingUncertaintyMs: mapped && finite(frame.uncertaintyNs) ? frame.uncertaintyNs / 1000000 : null,
    samplingIntervalMs: segment.frames[index + 1]
      ? segment.frames[index + 1].presentationTimeMs - frame.presentationTimeMs : null };
}

export async function fetchVerifiedMedia(path, reference, signal,
  { fetcher = globalThis.fetch, subtle = globalThis.crypto?.subtle } = {}) {
  if (!/^\/(?!\/)[a-zA-Z0-9/_-]+$/.test(path) || !/^[a-f0-9]{64}$/.test(reference.digest)
    || !Number.isInteger(reference.bytes) || reference.bytes < 1 || reference.bytes > MAX_MEDIA_BYTES || !subtle) {
    throw new Error("Media reference is unavailable");
  }
  const result = new Uint8Array(reference.bytes);
  for (let start = 0; start < reference.bytes; start += RANGE_BYTES) {
    if (signal.aborted) throw abortError();
    const end = Math.min(reference.bytes - 1, start + RANGE_BYTES - 1);
    const response = await fetcher(path, { credentials: "same-origin", cache: "no-store", redirect: "error", signal,
      headers: { Range: `bytes=${start}-${end}` } });
    const reject = async (message) => { await response.body?.cancel(); throw new Error(message); };
    if ([401, 403, 404, 410].includes(response.status)) return reject("Media access ended or evidence expired");
    if (response.status !== 206 || response.headers.get("Content-Range") !== `bytes ${start}-${end}/${reference.bytes}`
      || response.headers.get("Content-Length") !== String(end - start + 1)
      || response.headers.get("Content-Type") !== (reference.mimeType || "video/mp4")
      || response.headers.get("ETag") !== `"sha256-${reference.digest}"` || !response.body) {
      return reject("Media response does not match this recording");
    }
    const reader = response.body.getReader();
    let position = start;
    try {
      while (true) {
        if (signal.aborted) throw abortError();
        const { done, value } = await reader.read();
        if (done) break;
        if (position + value.length > end + 1) throw new Error("Media response exceeded its size");
        result.set(value, position); position += value.length;
      }
      if (position !== end + 1) throw new Error("Media response was interrupted");
    } finally { await reader.cancel(); reader.releaseLock(); }
  }
  const hash = new Uint8Array(await subtle.digest("SHA-256", result));
  if (signal.aborted) throw abortError();
  if (Array.from(hash, (byte) => byte.toString(16).padStart(2, "0")).join("") !== reference.digest) {
    throw new Error("Media checksum changed");
  }
  return result;
}

export class LatestMedia {
  constructor({ read = fetchVerifiedMedia, createURL = (blob) => URL.createObjectURL(blob),
    revokeURL = (url) => URL.revokeObjectURL(url) } = {}) {
    this.read = read; this.createURL = createURL; this.revokeURL = revokeURL;
    this.generation = 0; this.url = null; this.abort = null;
  }
  clear() {
    this.generation += 1; this.abort?.abort(); this.abort = null;
    if (this.url) this.revokeURL(this.url);
    this.url = null;
  }
  async load(path, reference) {
    this.clear();
    const generation = this.generation;
    const controller = new AbortController(); this.abort = controller;
    const timer = setTimeout(() => controller.abort(), 60000);
    try {
      const bytes = await this.read(path, reference, controller.signal);
      if (generation !== this.generation || controller.signal.aborted) return null;
      this.url = this.createURL(new Blob([bytes], { type: reference.mimeType || "video/mp4" }));
      return this.url;
    } catch (error) {
      if (generation !== this.generation) return null;
      throw error;
    } finally { clearTimeout(timer); }
  }
}

function videoReady(video, event, signal, ready) {
  if (signal.aborted) return Promise.reject(abortError());
  if (ready()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer); video.removeEventListener(event, done); video.removeEventListener("error", fail);
      signal.removeEventListener("abort", cancelled);
    };
    const done = () => { if (ready()) { cleanup(); resolve(); } };
    const fail = () => { cleanup(); reject(new Error("This video could not be decoded")); };
    const cancelled = () => { cleanup(); reject(abortError()); };
    const timer = setTimeout(fail, 20000);
    video.addEventListener(event, done); video.addEventListener("error", fail);
    signal.addEventListener("abort", cancelled, { once: true });
  });
}

export class SegmentPlayer {
  constructor(video, onStatus, { media = new LatestMedia() } = {}) {
    this.video = video; this.onStatus = onStatus; this.media = media;
    this.generation = 0; this.issueId = null; this.manifest = null; this.source = null;
    this.listeners = new AbortController(); this.seekAbort = null; this.frameCallback = null;
    video.addEventListener("timeupdate", () => this.tick(), { signal: this.listeners.signal });
    video.addEventListener("ended", () => {
      if (this.source) this.seek(this.source.displayInterval.endOffsetMs + 1);
    }, { signal: this.listeners.signal });
  }
  setIssue(issueId, manifest) {
    this.clear(); this.issueId = issueId; this.manifest = manifest;
  }
  clear() {
    this.generation += 1; this.seekAbort?.abort(); this.seekAbort = null;
    this.video.pause(); this.video.hidden = true;
    if (this.frameCallback !== null) this.video.cancelVideoFrameCallback?.(this.frameCallback);
    this.frameCallback = null; this.source = null; this.media.clear();
    this.video.removeAttribute("src"); this.video.load();
  }
  async seek(offsetMs) {
    this.generation += 1;
    const generation = this.generation;
    this.seekAbort?.abort(); this.seekAbort = new AbortController();
    const signal = this.seekAbort.signal;
    this.video.pause(); this.video.hidden = true;
    const point = locateTime(this.manifest, offsetMs);
    this.position = offsetMs;
    if (point.kind === "gap") { this.clear(); this.onStatus(point); return point; }
    this.onStatus({ ...point, kind: "loading" });
    try {
      if (this.source?.digest !== point.segment.digest) {
        this.source = null; this.video.removeAttribute("src"); this.video.load();
        const url = await this.media.load(`/api/release/issues/${this.issueId}/media/${point.segment.digest}`, point.segment);
        if (!url || generation !== this.generation) return null;
        this.video.src = url; this.video.load(); this.source = point.segment;
      }
      await videoReady(this.video, "loadedmetadata", signal, () => this.video.readyState >= 1);
      if (generation !== this.generation) return null;
      const seconds = point.presentationTimeMs / 1000;
      if (this.frameCallback !== null) this.video.cancelVideoFrameCallback?.(this.frameCallback);
      this.frameCallback = this.video.requestVideoFrameCallback?.((_now, metadata) => {
        this.frameCallback = null;
        if (generation !== this.generation || signal.aborted || !this.source) return;
        this.onStatus({ ...point, decodedPresentationTimeMs: metadata.mediaTime * 1000,
          seekErrorMs: Math.abs(metadata.mediaTime * 1000 - point.presentationTimeMs) });
      }) ?? null;
      if (Math.abs(this.video.currentTime - seconds) > .0005) {
        this.video.currentTime = seconds;
        await videoReady(this.video, "seeked", signal, () => !this.video.seeking && this.video.readyState >= 2);
      } else await videoReady(this.video, "loadeddata", signal, () => this.video.readyState >= 2);
      if (generation !== this.generation || signal.aborted) return null;
      this.video.hidden = false;
      this.onStatus({ ...point, currentTime: this.video.currentTime,
        width: this.video.videoWidth, height: this.video.videoHeight, readyState: this.video.readyState });
      return point;
    } catch (error) {
      if (generation !== this.generation || signal.aborted) return null;
      this.clear(); this.onStatus({ kind: "error", offsetMs, reason: error.message });
      return null;
    }
  }
  tick() {
    if (!this.source || this.video.hidden || this.video.paused) return;
    const offsetMs = this.source.displayInterval.startOffsetMs + this.video.currentTime * 1000;
    const point = locateTime(this.manifest, offsetMs);
    if (point.kind === "gap" || point.segment.digest !== this.source.digest) { this.seek(offsetMs); return; }
    this.position = offsetMs; this.onStatus(point);
  }
  async play() {
    if (!this.source || this.video.hidden) return;
    const generation = this.generation;
    try { await this.video.play(); }
    catch (error) { if (generation === this.generation) this.onStatus({ kind: "error", offsetMs: this.position, reason: "Playback could not start" }); }
  }
  pause() { this.video.pause(); }
  dispose() { this.clear(); this.listeners.abort(); this.issueId = null; this.manifest = null; }
}
