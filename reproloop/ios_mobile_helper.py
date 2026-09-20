"""Fixed, authority-bound HTTP control for an issued iOS XCTest helper.

The helper is deliberately a small adapter around :class:`TunnelClient`.
Endpoint and bearer-token material come only from the issued
``IOSXCTestLaunch``.  The adapter does not install applications, prove an
installed binary hash, sanitize a device, release native ownership, or
confirm authority receipts.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import math
import os
import re
import stat
import subprocess
import threading
import time

from . import contracts
from .execution.wire import canonical, decode_json
from .ios_mobile_identity import IOSInstalledIdentityObservation
from .ios_mobile_native import IOSMobileNativeOwner, require_native_dispatch
from .ios_mobile_runtime_identity import IOSRuntimeIdentityReadObservation
from .ios_mobile_xctest import IOSXCTestLaunch, IOSXCTestSession
from .ios_device_tools import IOSDeviceToolError
from .live.authority import DispatchPermit, HELPER_VERSION, NATIVE_PROTOCOL_VERSION
from .live.iphone import TunnelClient
from .repair_android_operation import _open_child_directory, _open_regular_at, _read_fd


_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_JOURNAL_ID = re.compile(r"[a-z][a-z0-9_-]{0,126}\Z")
_SAFE_ERROR = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_BUNDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_BUILD = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_NATIVE_CLOCK = "ios-mach-continuous"
_ACTIONS = frozenset({"tap", "long_press", "swipe", "text", "home", "launch", "terminate"})
_POINT_KEYS = frozenset(("x", "y"))
_SWIPE_KEYS = frozenset(("fromX", "fromY", "toX", "toY", "durationMs"))
_MAX_JOURNAL_BYTES = 512 * 1024
_MAX_TEXT_BYTES = 256
_MAX_HTTP_BODY_BYTES = 8 * 1024
_MAX_FRAME_BYTES = 3 * 1024 * 1024
_MAX_FRAME_WIRE_BYTES = 5 * 1024 * 1024
_MAX_NATIVE_TIME_MS = (2 ** 63 - 1) // 1_000_000
_MAX_CAPTURE_INTERVAL_MS = 10_000
_MAX_JOURNAL_OPERATIONS = 1024
_RESERVED_JOURNAL_OPERATIONS = 2
_MAX_STATUS_POLLS = 600
_MAX_ACK_POLLS = 600
_POLL_INTERVAL = 0.05


def _fail(code="ios_helper_control_unavailable"):
    """Raise an error whose public value contains no transport material."""
    raise IOSDeviceToolError(code)


def _is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _valid_id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _valid_digest(value):
    try:
        contracts.validate_digest(value)
    except Exception:
        return False
    return True


def _copy_payload(payload):
    if type(payload) is not dict:
        _fail("ios_helper_payload")
    try:
        copied = deepcopy(payload)
        body = canonical(copied)
    except Exception:
        _fail("ios_helper_payload")
    if len(body) > _MAX_HTTP_BODY_BYTES:
        _fail("ios_helper_payload")
    return copied


def _validate_point_payload(payload):
    if set(payload) != _POINT_KEYS or not all(_is_number(payload[key]) for key in _POINT_KEYS):
        _fail("ios_helper_payload")
    if not all(0 <= payload[key] <= 1 for key in _POINT_KEYS):
        _fail("ios_helper_payload")


def _validate_command_wire_payload(action, payload):
    """Validate the exact bounded shape consumed by LiveControlTests.swift."""
    if action == "tap":
        _validate_point_payload(payload)
    elif action == "long_press":
        if set(payload) != _POINT_KEYS | {"durationMs"}:
            _fail("ios_helper_payload")
        _validate_point_payload({"x": payload.get("x"), "y": payload.get("y")})
        if not _is_number(payload["durationMs"]) or not 50 <= payload["durationMs"] <= 3000:
            _fail("ios_helper_payload")
    elif action == "swipe":
        if set(payload) != _SWIPE_KEYS:
            _fail("ios_helper_payload")
        _validate_point_payload({"x": payload.get("fromX"), "y": payload.get("fromY")})
        _validate_point_payload({"x": payload.get("toX"), "y": payload.get("toY")})
        if not _is_number(payload["durationMs"]) or not 50 <= payload["durationMs"] <= 3000:
            _fail("ios_helper_payload")
    elif action == "text":
        if set(payload) != {"value"} or type(payload["value"]) is not str:
            _fail("ios_helper_payload")
        try:
            size = len(payload["value"].encode("utf-8"))
        except UnicodeError:
            _fail("ios_helper_payload")
        if size > _MAX_TEXT_BYTES:
            _fail("ios_helper_payload")
    elif action in {"home"}:
        if payload:
            _fail("ios_helper_payload")
    elif action in {"launch", "terminate"}:
        expected = {"applicationId"}
        if action == "launch":
            # Automatic runtime builds require the run nonce as well.  The
            # caller-facing command validator supplies the expected value.
            if "autoRunId" in payload:
                expected = {"applicationId", "autoRunId"}
                if type(payload.get("autoRunId")) is not str or _UUID.fullmatch(payload["autoRunId"]) is None:
                    _fail("ios_helper_payload")
        if set(payload) != expected or not _valid_id(payload.get("applicationId")):
            _fail("ios_helper_payload")
    else:
        _fail("ios_helper_action")


def command_payload(action, payload):
    """Return the pre-admission payload used by the Lab authority digest.

    Text values are intentionally represented by Lab's fixed ``live-text``
    variable marker;
    the wire command still carries the bounded value to the helper.  Cleanup
    has one fixed authority scope and never includes caller data.
    """
    if type(action) is not str:
        _fail("ios_helper_action")
    if action in {"cleanup", "authority_cleanup"}:
        if payload not in ({}, {"scope": "native-helper-and-pointers"}):
            _fail("ios_helper_payload")
        return {"kind": "cleanup", "payload": {"scope": "native-helper-and-pointers"}}
    if action not in _ACTIONS:
        _fail("ios_helper_action")
    copied = _copy_payload(payload)
    _validate_command_wire_payload(action, copied)
    if action == "text":
        return {"kind": "text", "payload": {"variable": "live-text"}}
    return {"kind": action, "payload": copied}


def _strict_mapping(actual, expected):
    """Compare a flat JSON authority mapping without bool/int coercion."""
    if type(actual) is not dict or set(actual) != set(expected):
        return False
    return all(type(actual[key]) is type(expected[key]) and actual[key] == expected[key]
               for key in expected)


def _write_record(parent, name, value, *, replace=False):
    """Write a canonical private JSON record relative to an owned directory."""
    if type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}\.json", name):
        _fail("ios_helper_journal")
    try:
        body = canonical(value)
    except Exception:
        _fail("ios_helper_journal")
    if len(body) > _MAX_JOURNAL_BYTES:
        _fail("ios_helper_journal")
    temporary = ".helper-record-" + os.urandom(16).hex()
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:])
            if count <= 0:
                _fail("ios_helper_journal")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        os.fsync(parent)
    except IOSDeviceToolError:
        raise
    except (OSError, ValueError, TypeError):
        _fail("ios_helper_journal")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _read_record(parent, name):
    try:
        descriptor = _open_regular_at(parent, name)
        try:
            body = _read_fd(descriptor, _MAX_JOURNAL_BYTES)
        finally:
            os.close(descriptor)
        value = decode_json(body)
        if canonical(value) != body:
            _fail("ios_helper_journal")
        return value
    except IOSDeviceToolError:
        raise
    except Exception:
        _fail("ios_helper_journal")


class IOSHelperChannel:
    """Control one already-started, authority-bound XCTest session."""

    def __init__(self, session: IOSXCTestSession):
        try:
            if type(session) is not IOSXCTestSession:
                _fail("ios_helper_session")
            runner = session.runner
            launch = session.launch
            if type(launch) is not IOSXCTestLaunch or launch._runner is not runner:
                _fail("ios_helper_session")
            owner = runner.native_owner
            if type(owner) is not IOSMobileNativeOwner:
                _fail("ios_helper_session")
            if runner._launches.get(id(launch)) is not launch or id(launch) not in runner._started:
                _fail("ios_helper_session")
            if runner._closed or session._closed:
                _fail("ios_helper_session")
            if type(launch._endpoint) is not str or not launch._endpoint:
                _fail("ios_helper_session")
            if type(launch._token) is not str or not launch._token or len(launch._token) > 512:
                _fail("ios_helper_session")
            self.session = session
            self.runner = runner
            self.launch = launch
            self.owner = owner
            # This is intentionally the sole transport construction site.  A
            # caller cannot override an endpoint, port, or bearer token.
            self._transport = TunnelClient(launch._endpoint, runner.tools.port, launch._token)
            self._lock = threading.RLock()
            self._handshake = None
            self._startup_permit = None
            self._status = None
            self._activated = False
            self._stopped = False
            self._closed = False
            self._last_sequence = 0
            self._last_frame_id = 0
            self._runtime_valid = False
            self._expected_runtime_run_id = None
            self._operations = {}
            self._operation_sequences = {}
            self._shutdown_result = None
            self._shutdown_permit = None
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_session")

    def __repr__(self):
        return "<IOSHelperChannel>"

    @property
    def native_handshake(self):
        return self._handshake

    @staticmethod
    def command_payload(action, payload):
        return command_payload(action, payload)

    def _session_payload(self):
        try:
            payload = self.launch.payload
            if type(payload) is not dict:
                _fail("ios_helper_session")
            return payload
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_session")

    def _bounds(self, cancellation, deadline_monotonic, *, allow_closed=False):
        if not callable(getattr(cancellation, "is_set", None)):
            _fail("ios_helper_bounds")
        if type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic):
            _fail("ios_helper_bounds")
        if cancellation.is_set():
            _fail("ios_helper_cancelled")
        if time.monotonic() >= deadline_monotonic:
            _fail("ios_helper_deadline")
        if self._closed and not allow_closed:
            _fail("ios_helper_closed")
        session_deadline = getattr(self.session, "deadline", None)
        if type(session_deadline) in (int, float) and math.isfinite(session_deadline):
            if time.monotonic() >= session_deadline:
                _fail("ios_helper_deadline")
            deadline_monotonic = min(deadline_monotonic, session_deadline)
        try:
            self.owner._check()
        except Exception:
            _fail("ios_helper_authority")
        return deadline_monotonic

    def _remaining_timeout(self, deadline_monotonic, ceiling):
        remaining = deadline_monotonic - time.monotonic()
        session_deadline = getattr(self.session, "deadline", None)
        if type(session_deadline) in (int, float) and math.isfinite(session_deadline):
            remaining = min(remaining, session_deadline - time.monotonic())
        if remaining <= 0:
            _fail("ios_helper_deadline")
        return min(float(ceiling), remaining)

    def _launch_digest(self):
        try:
            return contracts.digest(self._session_payload())
        except Exception:
            _fail("ios_helper_session")

    def _same_permit(self, left, right):
        fields = (
            "protocol_version", "operation_id", "operation_fingerprint", "payload_digest",
            "project_id", "session_id", "controller_id", "sequence", "ownership_generation",
            "host_incarnation", "helper_incarnation", "provider_incarnation", "deadline_ns",
        )
        # Both values have already passed the exact owner dispatch validator.
        return type(left) is type(right) and all(
            getattr(left, field, None) == getattr(right, field, None) for field in fields
        )

    def _check_permit(self, permit, cancellation, deadline_monotonic, payload_digest):
        deadline_monotonic = self._bounds(cancellation, deadline_monotonic)
        if not _valid_digest(payload_digest):
            _fail("ios_helper_authority")
        payload = self._session_payload()
        provider = payload.get("providerIncarnation")
        if not _valid_id(provider) or getattr(permit,'provider_incarnation',None) != provider:
            _fail("ios_helper_authority")
        try:
            self.runner._check_launch(self.launch)
            require_native_dispatch(self.owner, permit, payload_digest)
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_authority")
        return deadline_monotonic

    def _check_startup_permit(self, permit, cancellation, deadline_monotonic):
        launch_digest = self._launch_digest()
        self._check_permit(permit, cancellation, deadline_monotonic, launch_digest)
        dispatch = self.runner._dispatches.get(id(self.launch))
        if (type(dispatch) is not dict
                or dispatch.get("dispatchOperationId") != permit.operation_id
                or dispatch.get("permitFingerprint") != permit.operation_fingerprint
                or dispatch.get("payloadDigest") != permit.payload_digest):
            _fail("ios_helper_authority")
        if self._startup_permit is not None and not self._same_permit(permit, self._startup_permit):
            _fail("ios_helper_stale")
        return launch_digest

    def _validate_status(self, status, *, allow_initial=False):
        if type(status) is not dict:
            _fail("ios_helper_protocol")
        if (allow_initial and status.get("ready") is False and status.get("stopped") is False
                and status.get("capabilities") == {}
                and not any(key in status for key in (
                    "protocolVersion", "helperVersion", "nativeIncarnation", "targetBundle"))):
            return None
        allowed = {
            "ready", "stopped", "capabilities", "protocolVersion", "helperVersion",
            "helperIncarnation", "hostIncarnation", "providerIncarnation", "nativeIncarnation",
            "nativeClockId", "nativeTimeMs", "targetBundle", "applicationProfileDigest",
            "retirementVersion", "authoritySequence", "networkInterfaces",
        }
        if not set(status) <= allowed:
            _fail("ios_helper_protocol")
        interfaces = status.get("networkInterfaces")
        if interfaces is not None and (type(interfaces) is not list or len(interfaces) > 256
                or not all(type(item) is str and 0 < len(item) <= 64 for item in interfaces)):
            _fail("ios_helper_protocol")
        if {'retirementVersion', 'authoritySequence'} & set(status):
            if (type(status.get('retirementVersion')) is not int or status['retirementVersion'] != 1
                    or type(status.get('authoritySequence')) is not int
                    or not 0 <= status['authoritySequence'] < 2**53):
                _fail('ios_helper_protocol')
        if type(status.get("ready")) is not bool or type(status.get("stopped")) is not bool:
            _fail("ios_helper_protocol")
        if status["stopped"]:
            _fail("ios_helper_stopped")
        capabilities = status.get("capabilities")
        if type(capabilities) is not dict:
            _fail("ios_helper_protocol")
        cap_allowed = {
            "actions", "inputMode", "media", "nativeFrameBufferVersion",
            "nativeFrameBufferCapacity", "nativeFrameTimingVersion",
        }
        if not set(capabilities) <= cap_allowed:
            _fail("ios_helper_protocol")
        actions = capabilities.get("actions")
        payload = self._session_payload()
        expected_actions = payload.get("actions")
        if (type(actions) is not list or not actions or len(actions) != len(set(actions))
                or not all(type(item) is str and item in _ACTIONS for item in actions)
                or type(expected_actions) is not list or actions != expected_actions):
            _fail("ios_helper_protocol")
        if capabilities.get("inputMode") != "gesture-batch" or capabilities.get("media") != "sampled-jpeg":
            _fail("ios_helper_protocol")
        for key in ("nativeFrameBufferVersion", "nativeFrameBufferCapacity", "nativeFrameTimingVersion"):
            if key in capabilities and type(capabilities[key]) is not int:
                _fail("ios_helper_protocol")
        if "nativeFrameBufferVersion" in capabilities and capabilities["nativeFrameBufferVersion"] != 1:
            _fail("ios_helper_protocol")
        if "nativeFrameBufferCapacity" in capabilities and not 1 <= capabilities["nativeFrameBufferCapacity"] <= 4096:
            _fail("ios_helper_protocol")
        if type(status.get("protocolVersion")) is not int or status["protocolVersion"] != NATIVE_PROTOCOL_VERSION:
            _fail("ios_helper_protocol")
        if type(status.get("helperVersion")) is not int or status["helperVersion"] != HELPER_VERSION:
            _fail("ios_helper_protocol")
        for key in ("helperIncarnation", "hostIncarnation", "providerIncarnation", "nativeIncarnation", "nativeClockId"):
            if type(status.get(key)) is not str:
                _fail("ios_helper_protocol")
        if status["helperIncarnation"] != self.owner.helper_incarnation:
            _fail("ios_helper_authority")
        if status["providerIncarnation"] != payload.get("providerIncarnation"):
            _fail("ios_helper_authority")
        try:
            host_incarnation = self.owner.device._authority.host_incarnation
        except Exception:
            _fail("ios_helper_authority")
        if status["hostIncarnation"] != host_incarnation:
            _fail("ios_helper_authority")
        if not _valid_id(status["nativeIncarnation"]) or status["nativeClockId"] != _NATIVE_CLOCK:
            _fail("ios_helper_protocol")
        if (self._handshake is not None
                and (status["nativeIncarnation"] != self._handshake.native_incarnation
                     or status["nativeClockId"] != self._handshake.native_clock_id)):
            _fail("ios_helper_authority")
        if type(status.get("nativeTimeMs")) is not int or status["nativeTimeMs"] < 0:
            _fail("ios_helper_protocol")
        query_bundle = getattr(self.runner.query, "bundle", None)
        if type(query_bundle) is not str or status.get("targetBundle") != query_bundle:
            _fail("ios_helper_protocol")
        if status.get("applicationProfileDigest") != payload.get("profileDigest"):
            _fail("ios_helper_protocol")
        return status

    def _status_call(self, permit, cancellation, deadline_monotonic, *, allow_initial=False):
        digest = self._launch_digest()
        self._check_permit(permit, cancellation, deadline_monotonic, digest)
        timeout = self._remaining_timeout(deadline_monotonic, 2.0)
        try:
            value = self._transport.call("/status", timeout=timeout)
        except Exception:
            # During the startup poll the helper socket legitimately does not
            # exist yet: XCTest takes tens of seconds to install and launch
            # the test runner before its listener binds. Refusals inside the
            # initial window are "not ready", not a transport failure.
            if allow_initial:
                return None
            _fail("ios_helper_transport")
        self._check_permit(permit, cancellation, deadline_monotonic, digest)
        return self._validate_status(value, allow_initial=allow_initial)

    def _observation_bounds(self, cancellation, deadline_monotonic):
        deadline_monotonic = self._bounds(cancellation, deadline_monotonic)
        if self._handshake is None:
            _fail("ios_helper_handshake")
        if self._stopped:
            _fail("ios_helper_stopped")
        return deadline_monotonic

    def _observation_status_call(self, cancellation, deadline_monotonic):
        deadline_monotonic = self._observation_bounds(cancellation, deadline_monotonic)
        timeout = self._remaining_timeout(deadline_monotonic, 2.0)
        try:
            value = self._transport.call("/status", timeout=timeout)
        except Exception:
            _fail("ios_helper_transport")
        self._observation_bounds(cancellation, deadline_monotonic)
        status = self._validate_status(value)
        self._status = status
        return deepcopy(status)

    @staticmethod
    def _decode_frame_json(raw):
        if type(raw) is not bytes or len(raw) > _MAX_FRAME_WIRE_BYTES:
            _fail("ios_helper_frame")

        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    _fail("ios_helper_frame")
                value[key] = item
            return value

        def invalid(_):
            _fail("ios_helper_frame")

        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                               parse_constant=invalid)
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_frame")
        if type(value) is not dict:
            _fail("ios_helper_frame")
        return value

    def _declared_geometry(self):
        """Read an optional geometry declaration without accepting arbitrary paths."""
        payload = self._session_payload()
        candidates = []
        for key in ("runtimeProfile", "profile", "runtimeCapabilities"):
            selected = payload.get(key)
            if type(selected) is dict:
                candidates.append(selected)
        capabilities = payload.get("capabilities")
        if type(capabilities) is dict:
            candidates.append(capabilities)
        for candidate in candidates:
            geometry = candidate.get("geometry")
            if geometry is None and type(candidate.get("capabilities")) is dict:
                geometry = candidate["capabilities"].get("geometry")
            if geometry is None:
                continue
            if (type(geometry) is not dict
                    or set(geometry) != {"maxWidth", "maxHeight", "orientations"}
                    or type(geometry["maxWidth"]) is not int
                    or type(geometry["maxHeight"]) is not int
                    or not 1 <= geometry["maxWidth"] <= 8192
                    or not 1 <= geometry["maxHeight"] <= 8192
                    or type(geometry["orientations"]) is not list
                    or not geometry["orientations"]
                    or len(set(geometry["orientations"])) != len(geometry["orientations"])
                    or not all(item in {"portrait", "landscape"}
                               for item in geometry["orientations"])):
                _fail("ios_helper_frame")
            return geometry
        return None

    def _validate_frame(self, frame, cursor):
        required = {
            "id", "nativeFrameId", "imageBase64", "mime", "width", "height",
            "logicalWidth", "logicalHeight", "orientation", "capturedAt",
        }
        timing_enabled = (type(self._status) is dict
                           and self._status.get("capabilities", {}).get("nativeFrameTimingVersion") == 1)
        expected = required | ({"nativeTiming"} if timing_enabled else set())
        if type(frame) is not dict or set(frame) != expected:
            _fail("ios_helper_frame")
        native_id = frame.get("nativeFrameId")
        if (type(native_id) is not int or not 0 < native_id <= 2 ** 63 - 1
                or native_id <= cursor or native_id <= self._last_frame_id
                or frame.get("id") != "native-" + str(native_id)):
            _fail("ios_helper_frame")
        image = frame.get("imageBase64")
        if type(image) is not str or len(image) > 4 * ((_MAX_FRAME_BYTES + 2) // 3):
            _fail("ios_helper_frame")
        try:
            decoded = base64.b64decode(image, validate=True)
        except (ValueError, TypeError):
            _fail("ios_helper_frame")
        if (not decoded or len(decoded) > _MAX_FRAME_BYTES
                or base64.b64encode(decoded).decode("ascii") != image):
            _fail("ios_helper_frame")
        if frame.get("mime") != "image/jpeg":
            _fail("ios_helper_frame")
        for key in ("width", "height", "logicalWidth", "logicalHeight"):
            if type(frame.get(key)) is not int or not 0 < frame[key] <= 8192:
                _fail("ios_helper_frame")
        width, height = frame["width"], frame["height"]
        expected_orientation = "landscape" if width >= height else "portrait"
        if frame.get("orientation") != expected_orientation:
            _fail("ios_helper_frame")
        if (type(frame.get("capturedAt")) is not int
                or not 0 <= frame["capturedAt"] <= 2 ** 63 - 1):
            _fail("ios_helper_frame")
        geometry = self._declared_geometry()
        if geometry is not None and (width > geometry["maxWidth"]
                                     or height > geometry["maxHeight"]
                                     or frame["orientation"] not in geometry["orientations"]):
            _fail("ios_helper_frame")
        if timing_enabled:
            timing = frame["nativeTiming"]
            if (type(timing) is not dict
                    or set(timing) != {"version", "nativeClockId", "nativeIncarnation",
                                       "captureStartMs", "captureEndMs"}
                    or type(timing["version"]) is not int or timing["version"] != 1
                    or timing["nativeClockId"] != self._handshake.native_clock_id
                    or timing["nativeIncarnation"] != self._handshake.native_incarnation
                    or type(timing["captureStartMs"]) is not int
                    or type(timing["captureEndMs"]) is not int
                    or not 0 <= timing["captureStartMs"] <= _MAX_NATIVE_TIME_MS
                    or not 0 <= timing["captureEndMs"] <= _MAX_NATIVE_TIME_MS
                    or timing["captureStartMs"] < self._handshake.native_time_ms
                    or timing["captureEndMs"] < timing["captureStartMs"]
                    or timing["captureEndMs"] - timing["captureStartMs"] > _MAX_CAPTURE_INTERVAL_MS):
                _fail("ios_helper_frame")
        self._last_frame_id = native_id
        return frame

    def status(self, *, cancellation, deadline_monotonic):
        """Read status after handshake, including the pre-activation ready state.

        This read is allowed between handshake and activation so a caller can
        observe the helper startup window.  It issues no native authority.
        """
        with self._lock:
            try:
                return self._observation_status_call(cancellation, deadline_monotonic)
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_transport")

    def frame_after(self, cursor, *, cancellation, deadline_monotonic):
        """Read one strictly newer bounded native frame, or ``None`` if absent.

        A caller starts with cursor ``0`` after handshake; subsequent cursors
        must equal the last accepted native frame ID.  The frame keeps raw
        native timing for ``NativeFrameClock`` and is never host-mapped here.
        """
        with self._lock:
            try:
                deadline_monotonic = self._observation_bounds(cancellation, deadline_monotonic)
                if type(cursor) is not int or cursor < 0 or cursor > 2 ** 63 - 1:
                    _fail("ios_helper_frame")
                if cursor < self._last_frame_id:
                    _fail("ios_helper_stale_frame")
                if cursor > self._last_frame_id:
                    _fail("ios_helper_frame_cursor")
                timeout = self._remaining_timeout(deadline_monotonic, 2.0)
                try:
                    raw = self._transport.call(
                        "/frames/after/" + str(cursor),
                        timeout=timeout, binary=True,
                    )
                except Exception as error:
                    if getattr(error, "code", None) in {"device_bridge_error", "frame_unavailable"}:
                        self._last_frame_id = max(self._last_frame_id, cursor)
                        return None
                    _fail("ios_helper_transport")
                self._observation_bounds(cancellation, deadline_monotonic)
                frame = self._decode_frame_json(raw)
                return deepcopy(self._validate_frame(frame, cursor))
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_frame")

    def handshake(self, permit, *, cancellation, deadline_monotonic):
        """Read and bind one helper status under the issued launch permit."""
        with self._lock:
            try:
                launch_digest = self._check_startup_permit(permit, cancellation, deadline_monotonic)
                status = None
                for _ in range(_MAX_STATUS_POLLS):
                    status = self._status_call(
                        permit, cancellation, deadline_monotonic, allow_initial=True)
                    if status is not None:
                        break
                    bounded = self._bounds(cancellation, deadline_monotonic)
                    time.sleep(min(_POLL_INTERVAL, self._remaining_timeout(bounded, _POLL_INTERVAL)))
                if status is None:
                    _fail("ios_helper_deadline")
                self._check_startup_permit(permit, cancellation, deadline_monotonic)
                handshake = self.owner.bind_native_handshake(
                    permit,
                    protocol_version=status["protocolVersion"],
                    helper_version=status["helperVersion"],
                    helper_incarnation=status["helperIncarnation"],
                    provider_incarnation=status["providerIncarnation"],
                    native_incarnation=status["nativeIncarnation"],
                    native_clock_id=status["nativeClockId"],
                    native_time_ms=status["nativeTimeMs"],
                )
                if handshake.protocol_version != NATIVE_PROTOCOL_VERSION or handshake.helper_version != HELPER_VERSION:
                    _fail("ios_helper_protocol")
                self._startup_permit = permit
                self._handshake = handshake
                self._status = status
                self._last_sequence = permit.sequence
                # Reusing the same startup permit is valid only for activate;
                # the journal prevents another POST for a duplicate activate.
                self._operations.setdefault(permit.operation_id, {"sequence": permit.sequence, "kind": "startup"})
                self._operation_sequences[permit.operation_id] = permit.sequence
                return handshake
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_handshake")

    def _egress_policy_digest(self):
        try:
            return self.owner.operations.definition.egress_policy_digest
        except Exception:
            return None

    def _require_runtime_identity(self):
        payload = self._session_payload()
        runtime = payload.get("runtimeIdentity")
        keys={"bundleId", "buildId", "profileDigest", "runId"}
        policy=self.owner.operations.definition.sanitation_policy_digest
        if policy is not None:keys.add('sanitationPolicyDigest')
        egress=self._egress_policy_digest()
        if egress is not None:keys.add('egressPolicyDigest')
        if (type(runtime) is not dict or set(runtime) != keys
                or type(runtime.get("bundleId")) is not str or _BUNDLE.fullmatch(runtime.get("bundleId")) is None
                or type(runtime.get("buildId")) is not str or _BUILD.fullmatch(runtime.get("buildId")) is None
                or not _valid_digest(runtime.get("profileDigest"))
                or type(runtime.get("runId")) is not str or _UUID.fullmatch(runtime.get("runId")) is None
                or runtime.get('sanitationPolicyDigest') != policy
                or runtime.get('egressPolicyDigest') != egress):
            _fail("ios_helper_identity")
        return runtime

    def _validate_installed_identity(self, installed_identity):
        if type(installed_identity) is not IOSInstalledIdentityObservation:
            _fail("ios_helper_identity")
        payload = self._session_payload()
        runtime = self._require_runtime_identity()
        kind = installed_identity.command
        role_for_kind = {"install-candidate": "candidate", "restore-original": "original"}
        role = role_for_kind.get(kind)
        if role is None or payload.get("role") != role:
            _fail("ios_helper_identity")
        if self.owner._identity_results.get(kind) is not installed_identity:
            _fail("ios_helper_identity")
        if (installed_identity.native_binding_digest != self.owner.binding_digest
                or installed_identity.context_digest != self.owner.operation.context.digest
                or installed_identity.source_role != role):
            _fail("ios_helper_identity")
        app_digests = payload.get("appDigests")
        if (type(app_digests) is not dict or installed_identity.source_app_digest != app_digests.get(role)
                or installed_identity.bundle_id != runtime["bundleId"]):
            _fail("ios_helper_identity")
        if installed_identity.bundle_id != self.runner.query.bundle:
            _fail("ios_helper_identity")
        return runtime

    def _open_control(self):
        """Open helper-control using the issued launch directory as the root."""
        parent = None
        try:
            expected = json.loads(self.launch._identity)
            parent_root = self.session._operation_directory
            if parent_root is None:
                parent_root = self.owner.command_directory
            parent = _open_child_directory(parent_root, self.launch._work.name, expected=expected)
            try:
                os.mkdir("helper-control", 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            control = _open_child_directory(parent, "helper-control")
            info = os.fstat(control)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                os.close(control)
                _fail("ios_helper_journal")
            return control
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_journal")
        finally:
            if parent is not None:
                os.close(parent)

    @staticmethod
    def _journal_dir_name(key):
        if type(key) is not str or _JOURNAL_ID.fullmatch(key) is None:
            _fail("ios_helper_journal")
        name = "op-" + key
        if len(name) > 127:
            _fail("ios_helper_journal")
        return name

    def _open_journal_operation(self, key):
        control = self._open_control()
        try:
            name = self._journal_dir_name(key)
            try:
                os.mkdir(name, 0o700, dir_fd=control)
                os.fsync(control)
            except FileExistsError:
                pass
            operation = _open_child_directory(control, name)
            info = os.fstat(operation)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                os.close(operation)
                _fail("ios_helper_journal")
            return operation
        finally:
            os.close(control)

    def _journal_lookup(self, key, intent):
        operation = self._open_journal_operation(key)
        try:
            names = set(os.listdir(operation))
            if not names <= {"intent.json", "state.json", "ack.json"}:
                _fail("ios_helper_journal")
            if "intent.json" not in names:
                if names:
                    _fail("ios_helper_journal")
                return None
            saved = _read_record(operation, "intent.json")
            if saved != intent:
                _fail("ios_helper_stale")
            if "ack.json" not in names:
                state = _read_record(operation, "state.json") if "state.json" in names else {}
                if state.get("state") in {"uncertain", "attempted"}:
                    _fail("ios_helper_duplicate")
                _fail("ios_helper_journal")
            ack = _read_record(operation, "ack.json")
            if "state.json" not in names:
                _fail("ios_helper_journal")
            state = _read_record(operation, "state.json")
            if (type(state) is not dict
                    or set(state) != {"schemaVersion", "kind", "operationId", "state",
                                      "intentDigest", "ackDigest"}
                    or state.get("schemaVersion") != 1
                    or state.get("kind") != "ios-helper-control-state"
                    or state.get("operationId") != intent["operationId"]
                    or state.get("state") != "acknowledged"
                    or state.get("intentDigest") != contracts.digest(intent)
                    or state.get("ackDigest") != contracts.digest(ack)):
                _fail("ios_helper_journal")
            self._validate_ack_record(ack, intent)
            return ack
        finally:
            os.close(operation)

    def _validate_ack_record(self, ack, intent):
        base = {
            "schemaVersion", "kind", "operationId", "sequence", "ok", "timing",
            "errorCode", "authorityDigest", "responseDigest", "state"}
        expected = base | ({"cleanupEvidenceDigest"}
                           if intent.get("action") == "authority_cleanup"
                           and type(ack) is dict and ack.get("ok") is True else set())
        expected |= ({"networkEvidenceDigest"}
                     if intent.get("action") in {"activate", "authority_cleanup"}
                     and type(ack) is dict and ack.get("ok") is True
                     and "networkEvidenceDigest" in ack else set())
        if (type(ack) is not dict or set(ack) != expected
                or ack.get("schemaVersion") != 1
                or ack.get("kind") != "ios-helper-control-ack"
                or ack.get("operationId") != intent.get("operationId")
                or ack.get("sequence") != intent.get("sequence")
                or type(ack.get("ok")) is not bool
                or ack.get("timing") != "best-effort"
                or ack.get("state") != "acknowledged"
                or ack.get("errorCode") is not None and not _SAFE_ERROR.fullmatch(ack["errorCode"])
                or not _valid_digest(ack.get("authorityDigest"))
                or not _valid_digest(ack.get("responseDigest"))
                or intent.get("action") == "authority_cleanup"
                and ack.get("ok") is True
                and not _valid_digest(ack.get("cleanupEvidenceDigest"))
                or "networkEvidenceDigest" in ack
                and not _valid_digest(ack.get("networkEvidenceDigest"))
                # egress 바인딩된 cleanup ack는 카운터 다이제스트 없이 재생될 수 없다.
                or intent.get("action") == "authority_cleanup"
                and ack.get("ok") is True
                and self._egress_policy_digest() is not None
                and "networkEvidenceDigest" not in ack):
            _fail("ios_helper_journal")

    def _journal_capacity(self, key, *, reserved=False):
        """Bound retained operation directories before admitting a new POST."""
        control = self._open_control()
        try:
            operation_name = self._journal_dir_name(key)
            entries = os.listdir(control)
            count = 0
            exists = False
            for name in entries:
                if not name.startswith("op-"):
                    _fail("ios_helper_journal")
                operation = _open_child_directory(control, name)
                try:
                    info = os.fstat(operation)
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o700):
                        _fail("ios_helper_journal")
                finally:
                    os.close(operation)
                count += 1
                exists = exists or name == operation_name
            if exists:
                return True
            limit = (_MAX_JOURNAL_OPERATIONS if reserved else
                     _MAX_JOURNAL_OPERATIONS - _RESERVED_JOURNAL_OPERATIONS)
            if type(limit) is not int or limit <= 0 or count >= limit:
                _fail("ios_helper_journal_limit")
            return False
        finally:
            os.close(control)

    def _journal_begin(self, key, intent, *, reserved=False):
        try:
            existed = self._journal_capacity(key, reserved=reserved)
            existing = self._journal_lookup(key, intent)
            if existing is not None:
                return existing
            if existed:
                # An operation directory without an immutable intent is a
                # pre-POST crash fence; never reuse it for another effect.
                _fail("ios_helper_duplicate")
            operation = self._open_journal_operation(key)
            try:
                _write_record(operation, "intent.json", intent)
                _write_record(operation, "state.json", {
                    "schemaVersion": 1,
                    "kind": "ios-helper-control-state",
                    "operationId": intent["operationId"],
                    "state": "attempted",
                    "intentDigest": contracts.digest(intent),
                })
            finally:
                os.close(operation)
            return None
        except IOSDeviceToolError:
            raise
        except Exception:
            _fail("ios_helper_journal")

    def _journal_ack(self, key, intent, ack):
        operation = self._open_journal_operation(key)
        try:
            _write_record(operation, "ack.json", ack)
            _write_record(operation, "state.json", {
                "schemaVersion": 1,
                "kind": "ios-helper-control-state",
                "operationId": intent["operationId"],
                "state": "acknowledged",
                "intentDigest": contracts.digest(intent),
                "ackDigest": contracts.digest(ack),
            }, replace=True)
        finally:
            os.close(operation)

    def _journal_uncertain(self, key, intent):
        try:
            operation = self._open_journal_operation(key)
            try:
                _write_record(operation, "state.json", {
                    "schemaVersion": 1,
                    "kind": "ios-helper-control-state",
                    "operationId": intent["operationId"],
                    "state": "uncertain",
                    "intentDigest": contracts.digest(intent),
                }, replace=True)
            finally:
                os.close(operation)
        except Exception:
            # The effect is already uncertain; retain the primary bounded
            # transport error instead of exposing a filesystem exception.
            pass

    @staticmethod
    def _cached_result(ack):
        base = {
            "schemaVersion", "kind", "operationId", "sequence", "ok", "timing",
            "errorCode", "authorityDigest", "responseDigest", "state"}
        if (type(ack) is not dict
                or not base <= set(ack)
                or not set(ack) <= base | {"cleanupEvidenceDigest", "networkEvidenceDigest"}):
            _fail("ios_helper_journal")
        if type(ack["ok"]) is not bool or ack["timing"] != "best-effort":
            _fail("ios_helper_journal")
        if ack["errorCode"] is not None and not _SAFE_ERROR.fullmatch(ack["errorCode"]):
            _fail("ios_helper_journal")
        if ack["ok"]:
            result = {"ok": True, "timing": ack["timing"]}
            for field in ("cleanupEvidenceDigest", "networkEvidenceDigest"):
                if field in ack:
                    if not _valid_digest(ack[field]):
                        _fail("ios_helper_journal")
                    result[field] = ack[field]
            return result
        return {"ok": False, "outcome": "rejected", "code": ack["errorCode"] or "input_rejected"}

    def _make_intent(self, *, key, permit, action, canonical_value, wire_digest, authority_digest):
        return {
            "schemaVersion": 1,
            "kind": "ios-helper-control-intent",
            "recordId": key,
            "operationId": permit.operation_id,
            "sequence": permit.sequence,
            "action": action,
            "payloadDigest": contracts.digest(canonical_value),
            "wireDigest": wire_digest,
            "permitFingerprint": permit.operation_fingerprint,
            "authorityDigest": authority_digest,
            "launchPayloadDigest": self._launch_digest(),
            "nativeBindingDigest": self.owner.binding_digest,
        }

    @staticmethod
    def _check_network_evidence(value):
        from .ios_egress import network_counters
        try:
            network_counters(value)
        except Exception:
            _fail("ios_helper_protocol")

    def _validate_activation_ack(self, response, authority):
        expected = {"activated", "authority"}
        if self._egress_policy_digest() is not None:
            expected.add("networkEvidence")
        if (type(response) is not dict or set(response) != expected
                or response.get("activated") is not True
                or not _strict_mapping(response.get("authority"), authority)):
            _fail("ios_helper_protocol")
        if "networkEvidence" in response:
            self._check_network_evidence(response["networkEvidence"])

    def _activate_post(self, permit, cancellation, deadline_monotonic):
        authority = self.owner.native_grant(permit, self._handshake).wire()
        body = {"authority": authority}
        intent = self._make_intent(
            key=permit.operation_id, permit=permit, action="activate",
            canonical_value=self._session_payload(),
            wire_digest=contracts.digest(body), authority_digest=contracts.digest(authority),
        )
        existing = self._journal_begin(permit.operation_id, intent)
        if existing is not None:
            if (type(existing) is not dict
                    or existing.get("ok") is not True
                    or existing.get("authorityDigest") != contracts.digest(authority)):
                _fail("ios_helper_journal")
            self._cached_result(existing)
            result = {"ok": True, "activated": True}
            if "networkEvidenceDigest" in existing:
                result["networkEvidenceDigest"] = existing["networkEvidenceDigest"]
            return result
        try:
            self._check_startup_permit(permit, cancellation, deadline_monotonic)
            timeout = self._remaining_timeout(deadline_monotonic, 5.0)
            try:
                response = self._transport.call(
                    "/activate", body, timeout=timeout
                )
            except Exception:
                _fail("ios_helper_transport")
            self._check_startup_permit(permit, cancellation, deadline_monotonic)
            expected = self.owner.native_grant(permit, self._handshake).wire()
            self._validate_activation_ack(response, expected)
            ack = {
                "schemaVersion": 1,
                "kind": "ios-helper-control-ack",
                "operationId": permit.operation_id,
                "sequence": permit.sequence,
                "ok": True,
                "timing": "best-effort",
                "errorCode": None,
                "authorityDigest": contracts.digest(expected),
                "responseDigest": contracts.digest(response),
                "state": "acknowledged",
            }
            if "networkEvidence" in response:
                ack["networkEvidenceDigest"] = contracts.digest(response["networkEvidence"])
            self._journal_ack(permit.operation_id, intent, ack)
            result = {"ok": True, "activated": True}
            if "networkEvidence" in response:
                result["networkEvidence"] = deepcopy(response["networkEvidence"])
            self._operations[permit.operation_id] = result
            return result
        except IOSDeviceToolError:
            self._journal_uncertain(permit.operation_id, intent)
            raise
        except Exception:
            self._journal_uncertain(permit.operation_id, intent)
            _fail("ios_helper_transport")

    def _wait_ready(self, permit, cancellation, deadline_monotonic):
        for _ in range(_MAX_STATUS_POLLS):
            status = self._status_call(permit, cancellation, deadline_monotonic)
            self._status = status
            if status["ready"]:
                return status
            bounded = self._bounds(cancellation, deadline_monotonic)
            time.sleep(min(_POLL_INTERVAL, self._remaining_timeout(bounded, _POLL_INTERVAL)))
        _fail("ios_helper_deadline")

    def activate(self, permit, installed_identity, *, cancellation, deadline_monotonic):
        """Activate exactly once, then wait for XCTest's ready acknowledgement."""
        with self._lock:
            try:
                if self._handshake is None or self._startup_permit is None:
                    _fail("ios_helper_handshake")
                self._check_startup_permit(permit, cancellation, deadline_monotonic)
                self._validate_installed_identity(installed_identity)
                response = self._activate_post(permit, cancellation, deadline_monotonic)
                self._check_startup_permit(permit, cancellation, deadline_monotonic)
                self._wait_ready(permit, cancellation, deadline_monotonic)
                self._activated = True
                self._runtime_valid = True
                self._expected_runtime_run_id = None
                return dict(response, ready=True)
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_activation")

    def _require_runtime_observation(self):
        key = self._launch_digest()
        observation = self.owner._runtime_results.get(key)
        if type(observation) is not IOSRuntimeIdentityReadObservation:
            _fail("ios_helper_identity")
        runtime = self._require_runtime_identity()
        expected_run_id = self._expected_runtime_run_id or runtime["runId"]
        if (observation.launch_payload_digest != key
                or observation.context_digest != self.owner.operation.context.digest
                or observation.native_binding_digest != self.owner.binding_digest
                or observation.bundle_id != runtime["bundleId"]
                or observation.build_id != runtime["buildId"]
                or observation.profile_digest != runtime["profileDigest"]
                or observation.run_id != expected_run_id
                or observation.grade != "app-reported-runtime-id"
                or type(observation.started_at_ms) is not int
                or observation.started_at_ms < 0):
            _fail("ios_helper_identity")
        self._runtime_valid = True
        if runtime.get('sanitationPolicyDigest') is not None:
            if (self.owner._sanitation_results.get((key,'launch')) is not observation
                    or observation.sanitation is None
                    or observation.sanitation.stage != 'launch'
                    or observation.sanitation.policy_digest != runtime['sanitationPolicyDigest']):
                _fail('ios_helper_identity')
        return observation

    def _validate_action(self, action, payload, *, cleanup=False, expected_auto_run_id=None):
        if cleanup:
            if action not in {"authority_cleanup", "cleanup"}:
                _fail("ios_helper_payload")
            if action == "authority_cleanup" and payload:
                _fail("ios_helper_payload")
            if action == "cleanup" and payload not in ({}, {"scope": "native-helper-and-pointers"}):
                _fail("ios_helper_payload")
            return command_payload("cleanup", {})
        if type(action) is not str or action not in _ACTIONS:
            _fail("ios_helper_action")
        launch_actions = self._session_payload().get("actions")
        if type(launch_actions) is not list or action not in launch_actions:
            _fail("ios_helper_action")
        copied = _copy_payload(payload)
        if action == "launch" and self._session_payload().get("runtimeIdentity") is None:
            _fail("ios_helper_identity")
        if action == "launch" and self._session_payload().get("runtimeIdentity") is not None:
            runtime = self._require_runtime_identity()
            expected = {"applicationId", "autoRunId"}
            if (type(expected_auto_run_id) is not str
                    or _UUID.fullmatch(expected_auto_run_id) is None
                    or set(copied) != expected
                    or copied.get("autoRunId") != expected_auto_run_id):
                _fail("ios_helper_payload")
            if not _valid_id(copied.get("applicationId")):
                _fail("ios_helper_payload")
            if expected_auto_run_id == runtime["runId"]:
                _fail("ios_helper_stale")
        else:
            _validate_command_wire_payload(action, copied)
        return copied

    def _validate_command_ack(self, response, authority, operation_id, *, cleanup=False):
        if type(response) is not dict or response.get("pending") is not False:
            _fail("ios_helper_protocol")
        expected = {"pending", "id", "ok", "timing", "authority"}
        if "error" in response:
            expected.add("error")
        if cleanup and response.get("ok") is True:
            expected.add("cleanupEvidence")
            if self._egress_policy_digest() is not None:
                expected.add("networkEvidence")
        if set(response) != expected or response.get("id") != operation_id:
            _fail("ios_helper_protocol")
        if type(response.get("ok")) is not bool or response.get("timing") != "best-effort":
            _fail("ios_helper_protocol")
        if "error" in response and (type(response["error"]) is not str
                                     or not _SAFE_ERROR.fullmatch(response["error"])):
            _fail("ios_helper_protocol")
        if not _strict_mapping(response.get("authority"), authority):
            _fail("ios_helper_authority")
        if cleanup and response.get("ok") is True:
            evidence = response.get("cleanupEvidence")
            if (type(evidence) is not dict
                    or set(evidence) != {"bundleId", "state", "observer"}
                    or evidence.get("bundleId") != self.runner.query.bundle
                    or evidence.get("state") != "not-running"
                    or evidence.get("observer") != "xctest-application-state"):
                _fail("ios_helper_cleanup")
            if "networkEvidence" in response:
                self._check_network_evidence(response["networkEvidence"])

    def _invalidate_runtime_after(self, action, expected_auto_run_id):
        if action in {"launch", "terminate", "home"}:
            self._runtime_valid = False
            self._expected_runtime_run_id = (expected_auto_run_id
                                             if action == "launch" else None)

    def _command_impl(self, action, payload, permit, *, cancellation, deadline_monotonic,
                      cleanup=False, expected_auto_run_id=None):
        canonical_value = command_payload("cleanup" if cleanup else action, payload)
        payload_digest = contracts.digest(canonical_value)
        self._check_permit(permit, cancellation, deadline_monotonic, payload_digest)
        if self._startup_permit is None or self._handshake is None:
            _fail("ios_helper_handshake")
        if permit.sequence <= self._last_sequence and permit.operation_id not in self._operation_sequences:
            _fail("ios_helper_stale")
        if permit.operation_id == self._startup_permit.operation_id:
            _fail("ios_helper_stale")
        if not cleanup:
            if not self._activated:
                _fail("ios_helper_activation")
            if action == "launch":
                if expected_auto_run_id is None:
                    _fail("ios_helper_identity")
                if self._runtime_valid:
                    self._require_runtime_observation()
            else:
                if not self._runtime_valid:
                    _fail("ios_helper_identity")
                self._require_runtime_observation()
        if permit.operation_id in self._operation_sequences:
            if self._operation_sequences[permit.operation_id] != permit.sequence:
                _fail("ios_helper_stale")
        if cleanup:
            wire_action, wire_payload = "authority_cleanup", {}
        else:
            wire_action, wire_payload = action, deepcopy(payload)
        authority = self.owner.native_grant(permit, self._handshake).wire()
        body = {"id": permit.operation_id, "action": wire_action,
                "payload": wire_payload, "authority": authority}
        try:
            wire_digest = contracts.digest(body)
        except Exception:
            _fail("ios_helper_payload")
        intent = self._make_intent(
            key=permit.operation_id, permit=permit, action=wire_action,
            canonical_value=canonical_value, wire_digest=wire_digest,
            authority_digest=contracts.digest(authority),
        )
        existing = self._journal_begin(permit.operation_id, intent, reserved=cleanup)
        if existing is not None:
            # The permit was checked above, and no second mutation POST is made.
            retained = deepcopy(self._operations.get(permit.operation_id))
            result = retained if retained is not None else self._cached_result(existing)
            if cleanup and result.get("ok") is True:
                for field, digest_field in (("cleanupEvidence", "cleanupEvidenceDigest"),
                                            ("networkEvidence", "networkEvidenceDigest")):
                    evidence_digest = existing.get(digest_field)
                    evidence = result.get(field)
                    if evidence is not None:
                        if (not _valid_digest(evidence_digest)
                                or contracts.digest(evidence) != evidence_digest):
                            _fail("ios_helper_journal")
                    elif result.get(digest_field) != evidence_digest:
                        _fail("ios_helper_journal")
            expected = self.owner.native_grant(permit, self._handshake).wire()
            if existing.get("authorityDigest") != contracts.digest(expected):
                _fail("ios_helper_authority")
            # A persisted digest-only cleanup ack cannot recreate proof. Keep
            # a previously retained full observation in memory, but never
            # replace it with a digest-only reconstruction.
            if not (cleanup and "cleanupEvidence" not in result):
                self._operations[permit.operation_id] = result
            self._last_sequence = max(self._last_sequence, permit.sequence)
            if not cleanup:
                self._invalidate_runtime_after(action, expected_auto_run_id)
            return result
        if permit.operation_id in self._operation_sequences:
            # An in-memory duplicate without its durable acknowledgement must
            # never turn into a second mutation attempt.
            _fail("ios_helper_duplicate")
        try:
            self._check_permit(permit, cancellation, deadline_monotonic, payload_digest)
            timeout = self._remaining_timeout(deadline_monotonic, 5.0)
            try:
                accepted = self._transport.call(
                    "/command", body, timeout=timeout
                )
            except Exception:
                _fail("ios_helper_transport")
            self._check_permit(permit, cancellation, deadline_monotonic, payload_digest)
            if type(accepted) is not dict or set(accepted) != {"accepted"} or accepted.get("accepted") is not True:
                _fail("ios_helper_protocol")
            response = None
            for _ in range(_MAX_ACK_POLLS):
                self._check_permit(permit, cancellation, deadline_monotonic, payload_digest)
                try:
                    timeout = self._remaining_timeout(deadline_monotonic, 2.0)
                    response = self._transport.call(
                        "/ack/" + permit.operation_id,
                        timeout=timeout,
                    )
                except Exception:
                    _fail("ios_helper_transport")
                self._check_permit(permit, cancellation, deadline_monotonic, payload_digest)
                if type(response) is dict and response == {"pending": True}:
                    bounded = self._bounds(cancellation, deadline_monotonic)
                    time.sleep(min(_POLL_INTERVAL, self._remaining_timeout(bounded, _POLL_INTERVAL)))
                    continue
                expected = self.owner.native_grant(permit, self._handshake).wire()
                self._validate_command_ack(response, expected, permit.operation_id, cleanup=cleanup)
                break
            if response is None or response == {"pending": True}:
                _fail("ios_helper_deadline")
            result = ({"ok": True, "timing": "best-effort"} if response["ok"] else
                      {"ok": False, "outcome": "rejected",
                       "code": response.get("error") or "input_rejected"})
            if cleanup and response["ok"] is True:
                result["cleanupEvidence"] = deepcopy(response["cleanupEvidence"])
                if "networkEvidence" in response:
                    result["networkEvidence"] = deepcopy(response["networkEvidence"])
            ack = {
                "schemaVersion": 1,
                "kind": "ios-helper-control-ack",
                "operationId": permit.operation_id,
                "sequence": permit.sequence,
                "ok": result["ok"],
                "timing": "best-effort",
                "errorCode": result.get("code"),
                "authorityDigest": contracts.digest(expected),
                "responseDigest": contracts.digest(response),
                "state": "acknowledged",
            }
            if cleanup and response["ok"] is True:
                ack["cleanupEvidenceDigest"] = contracts.digest(result["cleanupEvidence"])
                if "networkEvidence" in result:
                    ack["networkEvidenceDigest"] = contracts.digest(result["networkEvidence"])
            self._journal_ack(permit.operation_id, intent, ack)
            self._operations[permit.operation_id] = result
            self._operation_sequences[permit.operation_id] = permit.sequence
            self._last_sequence = permit.sequence
            if not cleanup:
                self._invalidate_runtime_after(action, expected_auto_run_id)
            return result
        except IOSDeviceToolError:
            self._journal_uncertain(permit.operation_id, intent)
            raise
        except Exception:
            self._journal_uncertain(permit.operation_id, intent)
            _fail("ios_helper_transport")

    def command(self, action, payload, permit, *, cancellation, deadline_monotonic,
                expected_auto_run_id=None):
        """Issue one bounded helper action under its own dispatch permit."""
        with self._lock:
            try:
                cleanup = action in {"authority_cleanup", "cleanup"}
                if cleanup:
                    self._validate_action(action, payload, cleanup=True)
                else:
                    payload = self._validate_action(
                        action, payload, expected_auto_run_id=expected_auto_run_id)
                return self._command_impl(action, payload, permit,
                                          cancellation=cancellation,
                                          deadline_monotonic=deadline_monotonic,
                                          cleanup=cleanup,
                                          expected_auto_run_id=expected_auto_run_id)
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_command")

    def _stop(self, permit, *, cancellation, deadline_monotonic):
        key = "stop-" + permit.operation_id
        authority = self.owner.native_grant(permit, self._handshake).wire()
        body = {"authority": authority}
        intent = self._make_intent(
            key=key, permit=permit, action="stop",
            canonical_value=command_payload("cleanup", {}),
            wire_digest=contracts.digest(body), authority_digest=contracts.digest(authority),
        )
        existing = self._journal_begin(key, intent, reserved=True)
        if existing is not None:
            if (not existing.get("ok") or existing.get("timing") != "best-effort"
                    or existing.get("authorityDigest") != contracts.digest(authority)):
                _fail("ios_helper_journal")
            return {"stopped": True}
        try:
            self._check_permit(permit, cancellation, deadline_monotonic, permit.payload_digest)
            timeout = self._remaining_timeout(deadline_monotonic, 3.0)
            try:
                response = self._transport.call(
                    "/stop", body, timeout=timeout
                )
            except Exception:
                _fail("ios_helper_transport")
            self._check_permit(permit, cancellation, deadline_monotonic, permit.payload_digest)
            expected = self.owner.native_grant(permit, self._handshake).wire()
            if (type(response) is not dict or set(response) != {"stopped", "authority"}
                    or response.get("stopped") is not True
                    or not _strict_mapping(response.get("authority"), expected)):
                _fail("ios_helper_cleanup")
            ack = {
                "schemaVersion": 1,
                "kind": "ios-helper-control-ack",
                "operationId": permit.operation_id,
                "sequence": permit.sequence,
                "ok": True,
                "timing": "best-effort",
                "errorCode": None,
                "authorityDigest": contracts.digest(expected),
                "responseDigest": contracts.digest(response),
                "state": "acknowledged",
            }
            self._journal_ack(key, intent, ack)
            return {"stopped": True}
        except IOSDeviceToolError:
            self._journal_uncertain(key, intent)
            raise
        except Exception:
            self._journal_uncertain(key, intent)
            _fail("ios_helper_cleanup")

    def _wait_host(self, cancellation, deadline_monotonic):
        while self.session.host_process_running:
            bounded = self._bounds(cancellation, deadline_monotonic)
            time.sleep(min(_POLL_INTERVAL, self._remaining_timeout(bounded, _POLL_INTERVAL)))
        try:
            # This only reaps the XCTest host process and its collectors.  It
            # does not touch DeviceAuthority or issue a cleanup confirmation.
            reaped = self.session.close(deadline_monotonic=deadline_monotonic)
            active = self.session.active_processes
        except Exception:
            _fail("ios_helper_host")
        if not reaped or active != 0 or self.session.host_process_running \
                or self.session._process.returncode != 0:
            _fail("ios_helper_host")
        return {"terminated": True, "activeProcesses": 0, "evidence": "xctest-host-process"}

    def shutdown(self, permit, *, cancellation, deadline_monotonic):
        """Stop the helper after authorized cleanup and report separate lifetimes.

        The target observation is confirmed only when XCTest returns the
        cleanup-only application-state proof.  No native ownership or
        authority receipt is changed here.
        """
        with self._lock:
            if self._shutdown_result is not None:
                if self._shutdown_permit is None or not self._same_permit(permit, self._shutdown_permit):
                    _fail("ios_helper_stale")
                # A duplicate call returns the durable local observation and
                # never replays either POST.  The original result is sanitized.
                return deepcopy(self._shutdown_result)
            try:
                if self._handshake is None or self._startup_permit is None:
                    _fail("ios_helper_handshake")
                cleanup_payload = command_payload("cleanup", {})
                cleanup_digest = contracts.digest(cleanup_payload)
                self._check_permit(permit, cancellation, deadline_monotonic, cleanup_digest)
                if permit.sequence <= self._last_sequence and permit.operation_id not in self._operation_sequences:
                    _fail("ios_helper_stale")
                cleanup_result = self._command_impl(
                    "authority_cleanup", {}, permit,
                    cancellation=cancellation, deadline_monotonic=deadline_monotonic,
                    cleanup=True,
                )
                if cleanup_result.get("ok") is not True:
                    _fail("ios_helper_cleanup")
                target_evidence = cleanup_result.get("cleanupEvidence")
                target_confirmed = (type(target_evidence) is dict
                                    and set(target_evidence) == {"bundleId", "state", "observer"}
                                    and target_evidence.get("bundleId") == self.runner.query.bundle
                                    and target_evidence.get("state") == "not-running"
                                    and target_evidence.get("observer") == "xctest-application-state")
                stop_result = self._stop(permit, cancellation=cancellation,
                                         deadline_monotonic=deadline_monotonic)
                host = self._wait_host(cancellation, deadline_monotonic)
                result = {
                    "ok": True,
                    **({"networkEvidence": deepcopy(cleanup_result["networkEvidence"])}
                       if "networkEvidence" in cleanup_result else {}),
                    "host": dict(host, stopped=True),
                    "helper": {"stopped": stop_result["stopped"],
                                "terminated": stop_result["stopped"],
                                "terminationConfirmed": True,
                                "evidence": "stop-ack"},
                    "target": {"terminationRequested": True,
                                "terminated": target_confirmed,
                                "terminationConfirmed": target_confirmed,
                                "evidence": ("xctest-application-state"
                                             if target_confirmed else "cleanup-ack-without-observation")},
                }
                self._stopped = True
                self._shutdown_permit = permit
                self._shutdown_result = result
                self._closed = True
                return deepcopy(result)
            except IOSDeviceToolError:
                raise
            except Exception:
                _fail("ios_helper_cleanup")
            finally:
                # Do not retain the bearer token after shutdown, including an
                # uncertain stop.  A later caller must reconcile externally.
                try:
                    self._transport.token = ""
                except Exception:
                    pass


__all__ = ["IOSHelperChannel", "command_payload"]
