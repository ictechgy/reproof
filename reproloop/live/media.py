"""Binary, latest-frame Live media streaming primitives."""
from __future__ import annotations

import json
import math
import struct
import time
from typing import Any

from .model import LiveError, check


MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024
_MIMES = {"image/jpeg", "image/png", "image/svg+xml"}
_END_REASONS = {"session_closed", "session_failed", "stream_complete"}
_FRAME_BASE = {"type", "id", "geometryVersion", "width", "height",
               "orientation", "capturedAt", "mime"}
_RECORDING_FIELDS = {"objectDigest", "acquisitionSequence",
                     "presentationOffsetMs", "clockUncertaintyNs",
                     "captureTimingSource"}
_PROVIDER_FIELDS = {"providerClockId", "providerMonotonicNs", "nativeIncarnation"}


def _metadata_bytes(metadata: dict[str, Any]) -> bytes:
    check(isinstance(metadata, dict), "invalid_frame", "Frame metadata must be an object", 400)
    try:
        value = json.dumps(metadata, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise LiveError("invalid_frame", "Invalid frame metadata", 400) from None
    check(len(value) <= MAX_METADATA_BYTES, "invalid_frame", "Frame metadata is too large", 400)
    return value


def encode_frame(metadata: dict[str, Any], image: bytes = b"") -> bytes:
    """Encode one length-delimited media record without base64 or framing state."""
    check(isinstance(metadata, dict), "invalid_frame", "Frame metadata must be an object", 400)
    check(isinstance(image, bytes), "invalid_frame", "Frame image must be bytes", 400)
    check(len(image) <= MAX_IMAGE_BYTES, "invalid_frame", "Frame image is too large", 400)
    if metadata.get("type") == "frame":
        keys = set(metadata)
        recording = bool(keys & _RECORDING_FIELDS)
        provider = bool(keys & _PROVIDER_FIELDS)
        expected = _FRAME_BASE | (_RECORDING_FIELDS if recording else set()) | (_PROVIDER_FIELDS if provider else set())
        check(keys == expected and (not provider or recording),
              "invalid_frame", "Invalid frame metadata", 400)
        check(type(metadata["id"]) is int and metadata["id"] > 0,
              "invalid_frame", "Invalid frame id", 400)
        check(type(metadata["geometryVersion"]) is int and metadata["geometryVersion"] > 0,
              "invalid_frame", "Invalid frame geometry version", 400)
        check(type(metadata["width"]) is int and 0 < metadata["width"] <= 8192,
              "invalid_frame", "Invalid frame width", 400)
        check(type(metadata["height"]) is int and 0 < metadata["height"] <= 8192,
              "invalid_frame", "Invalid frame height", 400)
        check(metadata["orientation"] in {"portrait", "landscape"},
              "invalid_frame", "Invalid frame orientation", 400)
        check(type(metadata["capturedAt"]) is int and metadata["capturedAt"] >= 0,
              "invalid_frame", "Invalid frame timestamp", 400)
        check(metadata["mime"] in _MIMES, "invalid_frame", "Invalid frame mime", 400)
        check(bool(image), "invalid_frame", "Frame image cannot be empty", 400)
        if recording:
            check(isinstance(metadata["objectDigest"], str)
                  and len(metadata["objectDigest"]) == 64
                  and all(char in "0123456789abcdef" for char in metadata["objectDigest"]),
                  "invalid_frame", "Invalid frame object digest", 400)
            for field in ("acquisitionSequence", "presentationOffsetMs", "clockUncertaintyNs"):
                check(type(metadata[field]) is int and metadata[field] >= (1 if field == "acquisitionSequence" else 0),
                      "invalid_frame", "Invalid recording frame timing", 400)
            check(metadata["captureTimingSource"] in {
                "host-acquired", "provider-mapped", "native-unmapped"
            }, "invalid_frame", "Invalid recording frame timing source", 400)
        if provider:
            check(isinstance(metadata["providerClockId"], str)
                  and 0 < len(metadata["providerClockId"]) <= 64,
                  "invalid_frame", "Invalid provider clock identity", 400)
            check(type(metadata["providerMonotonicNs"]) is int
                  and metadata["providerMonotonicNs"] >= 0,
                  "invalid_frame", "Invalid provider monotonic timestamp", 400)
            check(isinstance(metadata["nativeIncarnation"], str)
                  and 0 < len(metadata["nativeIncarnation"]) <= 64,
                  "invalid_frame", "Invalid native incarnation", 400)
    elif metadata.get("type") == "end":
        check(set(metadata) == {"type", "reason"} and metadata["reason"] in _END_REASONS and not image,
              "invalid_frame", "Invalid stream end record", 400)
    else:
        raise LiveError("invalid_frame", "Invalid frame metadata", 400)
    encoded_metadata = _metadata_bytes(metadata)
    return struct.pack(">II", len(encoded_metadata), len(image)) + encoded_metadata + image


def _frame_record(frame: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    metadata = {key: frame[key] for key in
                ("id", "geometryVersion", "width", "height", "orientation", "capturedAt", "mime")}
    for key in _RECORDING_FIELDS | _PROVIDER_FIELDS:
        if key in frame:
            metadata[key] = frame[key]
    metadata["type"] = "frame"
    return metadata, bytes(frame["bytes"])


def _end_record(reason: str) -> bytes:
    return encode_frame({"type": "end", "reason": reason})


def serve_frames(handler: Any, lab: Any, sid: str, owner: str, *, duration: float = 30,
                 max_fps: float = 30, authorization=None, renew: bool = True) -> None:
    """Stream latest-only frames until expiry, session termination, or client close.

    The caller is responsible for the authenticated HTTP handler and server
    lifecycle.  Once headers are sent, failures are represented by an end
    record or a quiet disconnect; no HTTP error can be appended to the stream.
    """
    check(type(duration) in (int, float) and not isinstance(duration, bool)
          and math.isfinite(duration) and 0 < duration <= 30,
          "invalid_argument", "Stream duration must be between 0 and 30 seconds", 400)
    check(type(max_fps) in (int, float) and not isinstance(max_fps, bool)
          and math.isfinite(max_fps) and 0 < max_fps <= 30,
          "invalid_argument", "Stream FPS must be between 0 and 30", 400)
    check(authorization is None or callable(authorization),
          "invalid_argument", "Stream authorization is invalid", 400)
    check(type(renew) is bool, "invalid_argument", "Stream renewal policy is invalid", 400)
    if authorization is not None:
        authorization()
    session = lab._session(sid, owner)
    state = (lab.get_session(sid, owner) if renew
             else lab.peek_session(sid, owner))
    check(state["state"] in {"connecting", "active"}, "session_inactive", "Session is not active", 409)

    handler.protocol_version = "HTTP/1.0"
    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-repro-frames")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        connection = getattr(handler, "connection", None)
        if connection is not None and hasattr(connection, "settimeout"):
            connection.settimeout(5)
    except (OSError, AttributeError):
        pass

    deadline = time.monotonic() + float(duration)
    interval = 1.0 / float(max_fps)
    next_emit = 0.0
    last_frame_id = 0
    last_heartbeat = time.monotonic()
    end_reason = "stream_complete"
    try:
        while True:
            if authorization is not None:
                authorization()
            now = time.monotonic()
            if now >= deadline:
                break
            if renew and now - last_heartbeat >= 10:
                try:
                    if authorization is not None:
                        authorization()
                    lab.heartbeat(sid, owner, "media-viewer")
                    last_heartbeat = now
                except LiveError:
                    end_reason = "session_failed"
                    break

            with session["frameCondition"]:
                current = session.get("frame")
                current_id = current.get("id", 0) if current else 0
                current_state = session.get("state")
                if current_state in {"closed", "draining"}:
                    end_reason = "session_closed"
                    break
                if current_state == "failed":
                    end_reason = "session_failed"
                    break
                remaining = max(0.0, deadline - time.monotonic())
                if current is None or current_id <= last_frame_id:
                    # There is no frame to emit.  Do not derive this timeout
                    # from next_emit, which may already be in the past.
                    session["frameCondition"].wait(timeout=min(0.5, remaining))
                    continue
                if time.monotonic() < next_emit:
                    session["frameCondition"].wait(
                        timeout=min(0.5, max(0.0, next_emit - time.monotonic()), remaining))
                    continue
                metadata, image = _frame_record(current)
            packet = encode_frame(metadata, image)
            if authorization is not None:
                authorization()
            # No session or frame lock is held while writing to the client.
            handler.wfile.write(packet)
            handler.wfile.flush()
            last_frame_id = metadata["id"]
            next_emit = time.monotonic() + interval
        if authorization is not None:
            authorization()
        handler.wfile.write(_end_record(end_reason))
        handler.wfile.flush()
    except Exception:
        return
