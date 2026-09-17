"""Trusted native frame timing for the two native bridge directions.

The existing providerBootDigest field identifies a conservative helper-lifetime
clock segment here.  It is derived from a bound native incarnation, not a claim
that the device's OS boot identifier was measured.
"""
from __future__ import annotations

import hashlib
import secrets
import threading

from .authority import NativeHandshake
from .clock_sync import MAX_NS
from .model import check


def _milliseconds(value):
    check(type(value) is int and 0 <= value <= MAX_NS // 1_000_000,
          "native_timing_invalid", "Native clock value is invalid", 400)
    return value * 1_000_000


class NativeFrameClock:
    def __init__(self, synchronizer, anchor, check_observation):
        self.synchronizer = synchronizer
        self.anchor = anchor
        self.check_observation = check_observation
        self._lock = threading.RLock()
        self._enabled = None
        self._handshake = None
        self._mapping = None
        self._binding = None
        self._mapped_at = None
        self._pending = None
        self._closed = False
        self._buffered_frames = None
        self._buffer_capacity = None

    @property
    def buffered_frames(self):
        with self._lock:
            return self._buffered_frames is True

    @classmethod
    def for_session(cls, lab, sid, device_authority):
        if device_authority is None:
            return None
        recorder = lab._session(sid).get("releaseRecorder")
        anchor = recorder.anchor if recorder is not None else None
        synchronizer = anchor.synchronizer if anchor is not None else lab.authority.clock_sync
        return cls(synchronizer, anchor, device_authority.check_observation_authority)

    def _guard(self):
        check(not self._closed, "session_inactive", "Native clock is closed", 409)
        self.check_observation()

    def configure(self, handshake, capabilities):
        with self._lock:
            self._guard()
            check(type(handshake) is NativeHandshake and type(capabilities) is dict,
                  "native_timing_invalid", "Native clock handshake is unavailable", 400)
            version = capabilities.get("nativeFrameTimingVersion")
            check("nativeFrameTimingVersion" not in capabilities
                  or (type(version) is int and version == 1),
                  "native_timing_invalid", "Native frame timing version is unsupported", 400)
            enabled = version == 1
            buffer_version = capabilities.get("nativeFrameBufferVersion")
            check("nativeFrameBufferVersion" not in capabilities
                  or (type(buffer_version) is int and buffer_version == 1),
                  "native_timing_invalid", "Native frame buffer version is unsupported", 400)
            buffered = buffer_version == 1
            capacity = capabilities.get("nativeFrameBufferCapacity", 16)
            check(type(capacity) is int and 1 <= capacity <= 4096,
                  "native_timing_invalid", "Native frame buffer capacity is invalid", 400)
            check(self._enabled is None or (enabled == self._enabled
                  and handshake == self._handshake),
                  "native_timing_invalid", "Native clock handshake changed", 409)
            check(self._buffered_frames is None or buffered == self._buffered_frames,
                  "native_timing_invalid", "Native frame buffer support changed", 409)
            check(self._buffer_capacity is None or capacity == self._buffer_capacity,
                  "native_timing_invalid", "Native frame buffer capacity changed", 409)
            self._enabled = enabled
            self._handshake = handshake
            self._buffered_frames = buffered
            self._buffer_capacity = capacity

    def refresh_due(self):
        with self._lock:
            if self._enabled is False:
                return False
            self._guard()
            current = self.synchronizer.sample()
            if self._mapped_at is None or current._issuer is not self._mapped_at._issuer:
                return True
            elapsed = current.nanoseconds - self._mapped_at.nanoseconds
            if elapsed >= 30_000_000_000:
                return True
            # A slow initial round trip leaves less room for drift. Refresh
            # before using that headroom; every frame is still checked again.
            mapping = self._mapping
            drift = mapping.max_drift_ppm
            if drift >= 1_000_000:
                return True
            base = mapping.offset_upper_ns - mapping.offset_lower_ns
            denominator = 1_000_000 - drift
            horizon = ((elapsed + base) * 1_000_000 + denominator - 1) // denominator
            horizon += mapping.coordinator_receive_ns - mapping.coordinator_send_ns
            projected = base + 2 * ((horizon * drift + 999_999) // 1_000_000)
            return projected + 50_000_000 >= self.synchronizer.max_mapping_uncertainty_ns

    def _bind(self, mapping):
        self._guard()
        current = self.synchronizer.require_mapping(mapping)
        if self._mapping is not None:
            self.synchronizer.require_mapping(self._mapping)
            point = mapping.coordinator_receive_ns
            check(mapping.coordinator_send_ns >= self._mapping.coordinator_receive_ns,
                  "native_timing_invalid", "Native clock moved backward", 409)
            prior = self._mapping.translate(point)
            updated = mapping.translate(point)
            check(max(prior.earliest_ns, updated.earliest_ns)
                  <= min(prior.latest_ns, updated.latest_ns),
                  "native_timing_invalid", "Native clock mapping is discontinuous", 409)
        handshake = self._handshake
        binding = None
        if self.anchor is not None:
            epoch = hashlib.sha256(("native-frame-helper-epoch-v1\0"
                + handshake.native_clock_id + "\0" + handshake.native_incarnation).encode()).hexdigest()
            binding = self.anchor.bind_provider(mapping, provider_boot_digest=epoch,
                                                native_incarnation=handshake.native_incarnation)
        self._mapping, self._binding, self._mapped_at = mapping, binding, current

    def accept_status(self, handshake, status, host_sent, host_received):
        with self._lock:
            self.configure(handshake, status.get("capabilities"))
            if not self._enabled:
                return
            expected = {
                "protocolVersion": handshake.protocol_version,
                "helperVersion": handshake.helper_version,
                "helperIncarnation": handshake.helper_incarnation,
                "providerIncarnation": handshake.provider_incarnation,
                "nativeIncarnation": handshake.native_incarnation,
                "nativeClockId": handshake.native_clock_id,
            }
            check(all(type(status.get(k)) is type(v) and status[k] == v
                      for k, v in expected.items()),
                  "native_timing_invalid", "Native status clock identity changed", 409)
            native_ns = _milliseconds(status.get("nativeTimeMs"))
            check(native_ns >= handshake.native_time_ms * 1_000_000,
                  "native_timing_invalid", "Native status clock moved backward", 409)
            mapping = self.synchronizer.record_probe(
                coordinator_clock_id=handshake.native_clock_id, coordinator_ns=native_ns,
                host_sent=host_sent, host_received=host_received,
                coordinator_uncertainty_ns=handshake.mapping_uncertainty_ms * 1_000_000,
                max_drift_ppm=handshake.max_rate_error_ppm)
            self._bind(mapping)

    def begin_exchange(self, body):
        with self._lock:
            self._guard()
            check(self._enabled is True and type(body) is dict
                  and set(body) == {"nativeClockId", "nativeIncarnation", "nativeSendMs"}
                  and body["nativeClockId"] == self._handshake.native_clock_id
                  and body["nativeIncarnation"] == self._handshake.native_incarnation,
                  "native_timing_invalid", "Native clock exchange is invalid", 400)
            native_send = _milliseconds(body["nativeSendMs"])
            check(native_send >= self._handshake.native_time_ms * 1_000_000,
                  "native_timing_invalid", "Native clock moved backward", 409)
            received = self.synchronizer.sample()
            check(self._pending is None or
                  received.nanoseconds - self._pending[2].nanoseconds > 5_000_000_000,
                  "native_timing_invalid", "A native clock exchange is pending", 409)
            exchange_id = secrets.token_hex(16)
            self._pending = (exchange_id, native_send, received, self.synchronizer.sample())
            return {"ok": True, "exchangeId": exchange_id}

    def finish_exchange(self, body):
        with self._lock:
            self._guard()
            pending = self._pending
            check(type(body) is dict and set(body) == {"exchangeId", "nativeReceiveMs"}
                  and pending is not None and body["exchangeId"] == pending[0],
                  "native_timing_invalid", "Native clock exchange is unavailable", 409)
            self._pending = None  # Consume even a rejected completion; never replay it.
            native_receive = _milliseconds(body["nativeReceiveMs"])
            current = self.synchronizer.sample()
            check(current._issuer is pending[2]._issuer
                  and 0 <= current.nanoseconds - pending[2].nanoseconds <= 5_000_000_000,
                  "native_timing_invalid", "Native clock exchange expired", 409)
            handshake = self._handshake
            mapping = self.synchronizer.record_exchange(
                coordinator_clock_id=handshake.native_clock_id,
                coordinator_send_ns=pending[1], coordinator_receive_ns=native_receive,
                host_received=pending[2], host_sent=pending[3],
                coordinator_uncertainty_ns=handshake.mapping_uncertainty_ms * 1_000_000,
                max_drift_ppm=handshake.max_rate_error_ppm)
            self._bind(mapping)
            return {"ok": True}

    def frame_arguments(self, frame):
        with self._lock:
            self._guard()
            if self._enabled is False:
                return {"timing_source": "native-unmapped"}
            timing = frame.get("nativeTiming")
            check(self._enabled is True and self._mapping is not None
                  and type(timing) is dict and set(timing) == {
                      "version", "nativeClockId", "nativeIncarnation", "captureStartMs", "captureEndMs"}
                  and type(timing["version"]) is int and timing["version"] == 1
                  and timing["nativeClockId"] == self._handshake.native_clock_id
                  and timing["nativeIncarnation"] == self._handshake.native_incarnation
                  and type(frame.get("nativeFrameId")) is int and frame["nativeFrameId"] > 0,
                  "native_timing_invalid", "Native frame timing is unavailable", 409)
            start = _milliseconds(timing["captureStartMs"])
            end = _milliseconds(timing["captureEndMs"])
            check(0 <= end - start <= 10_000_000_000,
                  "native_timing_invalid", "Native capture interval is invalid", 400)
            current = self.synchronizer.require_mapping(self._mapping)
            check(self._mapping.translate(end).earliest_ns <=
                  current.nanoseconds + current.uncertainty_ns,
                  "native_timing_invalid", "Native capture time is in the future", 409)
            if self.anchor is None:
                return {"timing_source": "native-unmapped"}
            return {"timing_source": "provider-mapped",
                    "provider_clock_binding": self._binding,
                    "provider_monotonic_ns": end, "provider_capture_start_ns": start,
                    "native_incarnation": self._handshake.native_incarnation}

    def close(self):
        with self._lock:
            self._closed = True
            self._mapping = self._binding = self._pending = None
