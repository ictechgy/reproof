const HEADER_BYTES = 4;
const LENGTH_BYTES = 8;
const MAX_HEADER_BYTES = 16 * 1024;
const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
const MAX_BUFFER_BYTES = LENGTH_BYTES + MAX_HEADER_BYTES + MAX_IMAGE_BYTES;

const textDecoder = new TextDecoder("utf-8", { fatal: true });

function protocolError(message) {
  const error = new Error(message);
  error.name = "FrameStreamError";
  return error;
}

function asBytes(chunk) {
  if (chunk instanceof Uint8Array) return chunk;
  if (chunk instanceof ArrayBuffer) return new Uint8Array(chunk);
  if (ArrayBuffer.isView(chunk)) return new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  throw protocolError("Frame stream returned a non-binary chunk");
}

function uint32(bytes, offset) {
  return ((bytes[offset] * 0x1000000) + (bytes[offset + 1] << 16) +
    (bytes[offset + 2] << 8) + bytes[offset + 3]) >>> 0;
}

function validateHeader(header, bodyLength) {
  if (!header || typeof header !== "object" || Array.isArray(header)) throw protocolError("Frame header is not an object");
  if (header.type === "end") {
    if (bodyLength !== 0 || !["session_closed", "session_failed", "stream_complete"].includes(header.reason)) {
      throw protocolError("Invalid frame stream end marker");
    }
    return;
  }
  if (header.type !== "frame") throw protocolError("Unknown frame stream message");
  if (!Number.isInteger(header.id) || header.id < 1 || !Number.isInteger(header.geometryVersion) || header.geometryVersion < 1 ||
      !Number.isInteger(header.width) || header.width < 1 || header.width > 8192 ||
      !Number.isInteger(header.height) || header.height < 1 || header.height > 8192 ||
      !["portrait", "landscape"].includes(header.orientation) ||
      !Number.isInteger(header.capturedAt) || header.capturedAt < 0 || typeof header.mime !== "string" ||
      !["image/jpeg", "image/png", "image/svg+xml"].includes(header.mime)) {
    throw protocolError("Invalid frame metadata");
  }
  if (bodyLength < 1 || bodyLength > MAX_IMAGE_BYTES) throw protocolError("Frame image exceeds the limit");
}

/**
 * Consume a fetch body (or async iterable of Uint8Array chunks) as framed
 * messages. The yielded image bytes are bounded and detached from the input
 * chunk so callers may safely reuse a reader buffer.
 */
export async function* readIterator(source, options = {}) {
  const maxHeaderBytes = options.maxHeaderBytes || MAX_HEADER_BYTES;
  const maxImageBytes = options.maxImageBytes || MAX_IMAGE_BYTES;
  if (maxHeaderBytes > MAX_HEADER_BYTES || maxImageBytes > MAX_IMAGE_BYTES) throw protocolError("Frame stream bounds are too large");
  const reader = source && typeof source.getReader === "function" ? source.getReader() : null;
  const iterable = reader ? null : source?.[Symbol.asyncIterator]?.();
  if (!reader && !iterable) throw protocolError("Frame stream body is missing");
  let buffer = new Uint8Array(0);
  let expectedLength = null;
  let readerDone = false;
  let ended = false;
  let chunk = null;
  let offset = 0;
  const append = (bytes) => {
    const next = new Uint8Array(buffer.byteLength + bytes.byteLength);
    next.set(buffer);
    next.set(bytes, buffer.byteLength);
    buffer = next;
  };
  const readNext = async () => {
    const next = reader ? await reader.read() : await iterable.next();
    if (next.done) {
      readerDone = true;
      return false;
    }
    chunk = asBytes(next.value);
    offset = 0;
    return true;
  };
  try {
    while (true) {
      if (chunk === null || offset >= chunk.byteLength) {
        const needsBytes = buffer.byteLength < LENGTH_BYTES ||
          (expectedLength !== null && buffer.byteLength < expectedLength);
        if (needsBytes) {
          if (!(await readNext())) {
            if (!ended || buffer.byteLength) throw protocolError("Frame stream ended before a complete message");
            return;
          }
          if (chunk.byteLength === 0) continue;
        }
      }
      if (buffer.byteLength < LENGTH_BYTES) {
        const amount = Math.min(LENGTH_BYTES - buffer.byteLength, chunk.byteLength - offset);
        append(chunk.slice(offset, offset + amount));
        offset += amount;
        continue;
      }
      if (expectedLength === null) {
        const headerLength = uint32(buffer, 0);
        const bodyLength = uint32(buffer, HEADER_BYTES);
        if (headerLength < 2 || headerLength > maxHeaderBytes || bodyLength > maxImageBytes) {
          throw protocolError("Frame stream length exceeds the limit");
        }
        expectedLength = LENGTH_BYTES + headerLength + bodyLength;
      }
      const amount = Math.min(expectedLength - buffer.byteLength, chunk.byteLength - offset);
      if (buffer.byteLength + amount > MAX_BUFFER_BYTES) throw protocolError("Frame stream buffer exceeds the limit");
      append(chunk.slice(offset, offset + amount));
      offset += amount;
      if (buffer.byteLength < expectedLength) continue;

      const headerLength = uint32(buffer, 0);
      const bodyLength = uint32(buffer, HEADER_BYTES);
      const headerBytes = buffer.slice(LENGTH_BYTES, LENGTH_BYTES + headerLength);
      let header;
      try {
        header = JSON.parse(textDecoder.decode(headerBytes));
      } catch {
        throw protocolError("Frame stream header is not valid UTF-8 JSON");
      }
      const body = buffer.slice(LENGTH_BYTES + headerLength);
      validateHeader(header, bodyLength);
      buffer = new Uint8Array(0);
      expectedLength = null;
      if (header.type === "end") {
        ended = true;
        yield header;
        if (offset < chunk.byteLength) throw protocolError("Bytes follow the frame stream end marker");
        return;
      }
      yield { ...header, body };
    }
  } finally {
    if (reader && !readerDone) {
      try { await reader.cancel?.(); } catch { /* release still matters after a fetch abort */ }
    }
    reader?.releaseLock?.();
  }
}

export const decodeFramedStream = readIterator;
export const readiterator = readIterator;
export { MAX_HEADER_BYTES, MAX_IMAGE_BYTES, MAX_BUFFER_BYTES };
