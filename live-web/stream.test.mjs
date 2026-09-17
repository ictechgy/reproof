import test from "node:test";
import assert from "node:assert/strict";
import { decodeFramedStream, MAX_BUFFER_BYTES, MAX_HEADER_BYTES, MAX_IMAGE_BYTES } from "./stream.js";

function frame(header, body = new Uint8Array()) {
  const encoded = new TextEncoder().encode(JSON.stringify(header));
  const result = new Uint8Array(8 + encoded.length + body.length);
  new DataView(result.buffer).setUint32(0, encoded.length);
  new DataView(result.buffer).setUint32(4, body.length);
  result.set(encoded, 8);
  result.set(body, 8 + encoded.length);
  return result;
}

function split(bytes, sizes) {
  const chunks = [];
  let offset = 0;
  for (const size of sizes) {
    chunks.push(bytes.slice(offset, offset + size));
    offset += size;
  }
  if (offset < bytes.length) chunks.push(bytes.slice(offset));
  return chunks;
}

function readerFrom(chunks) {
  let index = 0;
  const control = { cancelled: false, released: false };
  return {
    control,
    getReader() {
      return {
        async read() {
          if (index >= chunks.length) return { done: true, value: undefined };
          return { done: false, value: chunks[index++] };
        },
        async cancel() { control.cancelled = true; },
        releaseLock() { control.released = true; },
      };
    },
  };
}

async function collect(source) {
  const messages = [];
  for await (const message of decodeFramedStream(source)) messages.push(message);
  return messages;
}

const frameHeader = { type: "frame", id: 1, geometryVersion: 2, width: 400, height: 800,
  orientation: "portrait", capturedAt: 123, mime: "image/png" };

test("decodes split headers and bodies across arbitrary reader chunks", async () => {
  const image = Uint8Array.from([1, 2, 3, 4]);
  const bytes = new Uint8Array([...frame(frameHeader, image), ...frame({ type: "end", reason: "stream_complete" })]);
  const messages = await collect(readerFrom(split(bytes, [1, 2, 3, 5, 7, 11])));
  assert.equal(messages.length, 2);
  assert.deepEqual(messages[0].body, image);
  assert.equal(messages[0].geometryVersion, 2);
  assert.deepEqual(messages[1], { type: "end", reason: "stream_complete" });
});

test("rejects malformed lengths, JSON, and end markers", async () => {
  const malformedLength = new Uint8Array(8);
  new DataView(malformedLength.buffer).setUint32(0, MAX_HEADER_BYTES + 1);
  await assert.rejects(collect(readerFrom([malformedLength])), /length exceeds/);

  const malformedJson = new Uint8Array([0, 0, 0, 2, 0, 0, 0, 0, 0x7b, 0x7b]);
  await assert.rejects(collect(readerFrom([malformedJson])), /valid UTF-8 JSON/);

  const badEnd = frame({ type: "end", reason: "unknown" });
  await assert.rejects(collect(readerFrom([badEnd])), /end marker/);
});

test("rejects oversized image and bounded reader chunks", async () => {
  const oversized = new Uint8Array(8);
  new DataView(oversized.buffer).setUint32(0, 2);
  new DataView(oversized.buffer).setUint32(4, MAX_IMAGE_BYTES + 1);
  await assert.rejects(collect(readerFrom([oversized])), /length exceeds/);

  const giant = new Uint8Array(MAX_BUFFER_BYTES + 1);
  await assert.rejects(collect(readerFrom([giant])), /length exceeds|buffer exceeds/);
});

test("accepts coalesced maximum-size frames without unbounded buffering", async () => {
  const large = new Uint8Array(MAX_IMAGE_BYTES);
  large[0] = 7;
  const first = frame({ ...frameHeader, id: 11 }, large);
  const second = frame({ ...frameHeader, id: 12 }, large);
  const end = frame({ type: "end", reason: "stream_complete" });
  const messages = await collect(readerFrom([new Uint8Array([...first, ...second, ...end])]));
  assert.deepEqual(messages.map((message) => message.id ?? message.reason), [11, 12, "stream_complete"]);
  assert.equal(messages[0].body.length, MAX_IMAGE_BYTES);
  assert.equal(messages[1].body.length, MAX_IMAGE_BYTES);
});

test("rejects EOF before a complete message", async () => {
  const bytes = frame(frameHeader, Uint8Array.from([1, 2, 3]));
  await assert.rejects(collect(readerFrom([bytes.slice(0, -1)])), /before a complete message/);
});

test("cancels and releases the reader after an end marker", async () => {
  const source = readerFrom([frame({ type: "end", reason: "stream_complete" })]);
  await collect(source);
  assert.equal(source.control.cancelled, true);
  assert.equal(source.control.released, true);
});
