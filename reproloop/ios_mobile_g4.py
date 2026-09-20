"""Bounded iOS G4 replay bridge over one issued XCTest owner.

The Lab and ScenarioRunner run on ordinary service threads.  The XCTest
runner, helper channel, runtime reader, and native frame clock remain owned by
one callback thread, which is driven by :class:`IOSOwnerCommandPump`.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import base64
import math
import queue
import threading
import time

from . import contracts
from .ios_mobile_helper import IOSHelperChannel
from .ios_mobile_identity import IOSInstalledIdentityObservation
from .ios_mobile_native import IOSMobileNativeOwner
from .ios_mobile_xctest import IOSXCTestLaunch, IOSXCTestRunner
from .ios_profile import IosAppProfile
from .live.issue_sessions import IssueSessionService
from .live.model import Lab, LiveError
from .live.native_frame_clock import NativeFrameClock
from .live.recording_session import TrustedProjectRegistration
from .qualification import ApprovedExecution
from .live.providers import require_runtime_frame
from .repair_callbacks import CallbackCancellation


_PUMP_QUEUE_SIZE = 32
_PUMP_WAIT_SECONDS = 0.01
_FRAME_BATCH = 8
_ALLOWED_ACTIONS = frozenset({"tap", "long_press", "swipe", "text", "home"})


class IOSG4Error(LiveError):
    """A bounded bridge failure with no native transport detail."""

    def __init__(self, code, message="iOS G4 bridge failed", status=409, *, unknown=False):
        self.unknown = unknown
        super().__init__(code, message, status)


class _RequestKind(Enum):
    START = "start"
    EXECUTE = "execute"
    CLOSE = "close"


@dataclass(slots=True)
class _OwnerRequest:
    kind: _RequestKind
    cancellation: object
    deadline_monotonic: float
    permit: object
    session: dict | None = None
    lab: Lab | None = None
    action: str | None = None
    payload: dict | None = None
    event: threading.Event = None
    started: bool = False
    abandoned: bool = False
    completed: bool = False
    result: object = None
    error: BaseException | None = None

    def __post_init__(self):
        if self.event is None:
            self.event = threading.Event()


def _require(condition, code="ios_g4_invalid", message="iOS G4 input is invalid", status=400):
    if not condition:
        raise IOSG4Error(code, message, status)


def _bounds(cancellation, deadline_monotonic):
    _require(callable(getattr(cancellation, "is_set", None)),
             "ios_g4_bounds", "iOS G4 cancellation is invalid")
    _require(type(deadline_monotonic) in (int, float)
             and math.isfinite(deadline_monotonic),
             "ios_g4_bounds", "iOS G4 deadline is invalid")
    if cancellation.is_set():
        raise IOSG4Error("cancelled", "iOS G4 operation was cancelled", 409)
    if time.monotonic() >= deadline_monotonic:
        raise IOSG4Error("deadline_exceeded", "iOS G4 operation deadline elapsed", 409)


class IOSOwnerCommandPump:
    """Bounded fixed-request queue executed by one native owner thread."""

    def __init__(self, provider, *, max_pending=_PUMP_QUEUE_SIZE):
        _require(type(max_pending) is int and 1 <= max_pending <= 256,
                 "ios_g4_queue", "iOS G4 request queue size is invalid")
        self._provider = provider
        self._queue = queue.Queue(maxsize=max_pending)
        self._registry = {}
        self._lock = threading.RLock()
        self._owner_thread_id = None
        self._owner_thread = None
        self._closed = False
        self._aborted = False
        self._fault = None

    @property
    def owner_thread_id(self):
        with self._lock:
            return self._owner_thread_id

    @property
    def pending(self):
        with self._lock:
            return len(self._registry)

    @property
    def fault(self):
        with self._lock:
            return self._fault

    def _bind_owner_thread(self):
        current_thread = threading.current_thread()
        current = threading.get_ident()
        with self._lock:
            if self._owner_thread_id is None:
                self._owner_thread_id = current
                self._owner_thread = current_thread
            elif (self._owner_thread_id != current
                  or self._owner_thread is not current_thread):
                raise IOSG4Error("ios_g4_owner_thread",
                                 "Native owner callbacks moved threads", 409)

    def _new_request(self, kind, *, cancellation, deadline_monotonic, permit,
                     session=None, lab=None, action=None, payload=None):
        _require(type(kind) is _RequestKind, "ios_g4_request",
                 "Unknown iOS G4 request")
        _bounds(cancellation, deadline_monotonic)
        request = _OwnerRequest(kind, cancellation, deadline_monotonic, permit,
                                session=session, lab=lab, action=action,
                                payload=payload)
        with self._lock:
            if self._closed or self._aborted:
                raise IOSG4Error("ios_g4_closed", "iOS G4 owner pump is closed", 409)
            self._registry[id(request)] = request
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                self._registry.pop(id(request), None)
                raise IOSG4Error("ios_g4_queue_full",
                                 "iOS G4 owner queue is full", 409) from None
        return request

    def _abandon(self, request, code, message):
        with self._lock:
            if self._registry.get(id(request)) is not request or request.completed:
                return
            request.abandoned = True
            if request.error is None:
                request.error = IOSG4Error(code, message, 409,
                                           unknown=request.started)
            request.event.set()

    def _finish(self, request, *, result=None, error=None):
        with self._lock:
            if self._registry.get(id(request)) is not request:
                return
            request.completed = True
            if request.error is None:
                if request.abandoned:
                    request.error = IOSG4Error(
                        "ios_g4_unknown" if request.started else "cancelled",
                        "iOS G4 request was abandoned", 409,
                        unknown=request.started)
                elif error is not None:
                    request.error = error
                else:
                    request.result = result
            self._registry.pop(id(request), None)
            request.event.set()

    def submit(self, kind, *, cancellation, deadline_monotonic, permit,
               session=None, lab=None, action=None, payload=None):
        """Submit one fixed enum request and wait for its owner-thread result."""
        request = self._new_request(
            kind, cancellation=cancellation, deadline_monotonic=deadline_monotonic,
            permit=permit, session=session, lab=lab, action=action, payload=payload)
        while not request.event.wait(min(_PUMP_WAIT_SECONDS,
                                         max(0.001, deadline_monotonic - time.monotonic()))):
            if cancellation.is_set():
                self._abandon(request, "cancelled", "iOS G4 request was cancelled")
                raise IOSG4Error("cancelled", "iOS G4 request was cancelled", 409)
            if time.monotonic() >= deadline_monotonic:
                self._abandon(request, "deadline_exceeded",
                              "iOS G4 request deadline elapsed")
                raise IOSG4Error("deadline_exceeded",
                                 "iOS G4 request deadline elapsed", 409)
        with self._lock:
            error, result = request.error, request.result
        if error is not None:
            raise error
        return result

    def abandon_all(self, code="ios_g4_unknown", message="iOS G4 owner work is unknown"):
        with self._lock:
            values = tuple(self._registry.values())
        for request in values:
            self._abandon(request, code, message)

    def _dequeue(self):
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def pump_once(self, *, deadline_monotonic=None):
        """Execute at most one request and one bounded observation tick."""
        self._bind_owner_thread()
        if deadline_monotonic is not None:
            _bounds(threading.Event(), deadline_monotonic)
        request = self._dequeue()
        processed = request is not None
        request_result = None
        request_error = None
        if request is not None:
            with self._lock:
                valid = self._registry.get(id(request)) is request
                abandoned = request.abandoned
                if valid and not abandoned:
                    request.started = True
            if not valid:
                self._queue.task_done()
            elif abandoned:
                self._finish(request)
                self._queue.task_done()
            else:
                try:
                    _bounds(request.cancellation, request.deadline_monotonic)
                    request_result = self._provider._execute_owner_request(request)
                except BaseException as error:
                    request_error = error
                finally:
                    self._queue.task_done()
        observation_error = None
        try:
            self._provider._poll_owner(deadline_monotonic)
        except BaseException as error:
            observation_error = error
            with self._lock:
                self._fault = error
            self.abandon_all("ios_g4_unknown", "iOS G4 owner observation is unknown")
        if request is not None and valid and not abandoned:
            if observation_error is not None and request_error is None:
                request_error = IOSG4Error(
                    "ios_g4_unknown", "iOS G4 owner observation is unknown", 409,
                    unknown=True)
            self._finish(request, result=request_result, error=request_error)
        if observation_error is not None:
            raise observation_error
        return processed

    def pump_until_idle(self, *, deadline_monotonic):
        self._bind_owner_thread()
        while time.monotonic() < deadline_monotonic:
            processed = self.pump_once(deadline_monotonic=deadline_monotonic)
            if not processed and self.pending == 0:
                return
        self.abandon_all("deadline_exceeded", "iOS G4 owner queue did not settle")

    def pump(self, *, deadline_monotonic=None, max_requests=1):
        """Pump a bounded number of fixed requests on the owner callback thread."""
        _require(type(max_requests) is int and 1 <= max_requests <= 256,
                 "ios_g4_queue", "iOS G4 pump bound is invalid")
        count = 0
        while count < max_requests:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
            if not self.pump_once(deadline_monotonic=deadline_monotonic):
                break
            count += 1
        return count

    def close(self):
        with self._lock:
            self._closed = True
        self.abandon_all("ios_g4_closed", "iOS G4 owner pump is closed")

    def abort(self):
        with self._lock:
            self._aborted = True
        self.abandon_all("ios_g4_unknown", "iOS G4 owner work is unknown")


class IOSG4Provider:
    """Lab provider for one issued XCTest launch and one native owner."""

    def __init__(self, runner, launch, installed_identity, profile, native_owner,
                 *, max_pending=_PUMP_QUEUE_SIZE):
        _require(type(runner) is IOSXCTestRunner,
                 "ios_g4_owner", "Issued XCTest runner is required")
        _require(type(launch) is IOSXCTestLaunch and launch._runner is runner
                 and runner._launches.get(id(launch)) is launch,
                 "ios_g4_launch", "Issued XCTest launch is required")
        _require(type(installed_identity) is IOSInstalledIdentityObservation,
                 "ios_g4_identity", "Issued installed identity is required")
        _require(type(profile) is IosAppProfile,
                 "ios_g4_profile", "Validated iOS app profile is required")
        owner = native_owner
        _require(type(owner) is IOSMobileNativeOwner and runner.native_owner is owner,
                 "ios_g4_owner", "Current native owner is required")
        payload = launch.payload
        _require(type(payload) is dict
                 and payload.get("kind") == "ios-fixed-xctest-launch-v1"
                 and payload.get("applicationId") == profile.data["applicationId"]
                 and profile.data.get("projectDigest")
                     == owner.operations.definition.project_digest
                 and payload.get("profileDigest") == profile.digest
                 and payload.get("queryDefinitionDigest") == runner.query.definition_digest
                 and payload.get("nativeBindingDigest") == owner.binding_digest
                 and payload.get("contextDigest") == owner.operation.context.digest
                 and payload.get("projectDigest") == owner.operations.definition.project_digest
                 and payload.get("scopeDigest") == owner.operations.definition.scope_digest
                 and payload.get("role") == installed_identity.source_role
                 and payload.get("appDigests", {}).get(installed_identity.source_role)
                     == installed_identity.source_app_digest,
                 "ios_g4_binding", "XCTest launch is not bound to the selected owner")
        _require(owner._identity_results.get(installed_identity.command) is installed_identity,
                 "ios_g4_identity", "Installed identity is not an issued observation")
        self._validate_selected_artifact(owner, launch, installed_identity, profile)
        capabilities = profile.data["capabilities"]
        _require("pixels" in capabilities["observations"]
                 and capabilities["captureAdapter"] == {"id": "native-frame", "version": 1}
                 and capabilities["locator"] is None
                 and "accessibility" not in capabilities["observations"]
                 and "logs" not in capabilities["observations"],
                 "ios_g4_profile", "Profile declares unsupported G4 observations")
        actions = payload.get("actions")
        _require(type(actions) is list and set(actions) <= set(capabilities["actions"]),
                 "ios_g4_profile", "XCTest actions differ from the selected profile")
        self.runner = runner
        self.launch = launch
        self.installed_identity = installed_identity
        self.profile = profile
        self.native_owner = owner
        self.definition = runner.query
        self.device_authority = None
        self.provider_incarnation = None
        self._pump = IOSOwnerCommandPump(self, max_pending=max_pending)
        self.owner_pump = self._pump
        self._context_cancellation = threading.Event()
        self._context_deadline = time.monotonic() + 60
        self._lab = None
        self._sid = None
        self._session = None
        self._helper = None
        self._runtime_reader = None
        self._clock = None
        self._handshake = None
        self._last_frame_id = 0
        self._started = False
        self._closed = False
        self._close_result = None
        self._close_permit = None
        self._fault = None
        self._network_start = None
        self._network_end = None

    @staticmethod
    def _validate_selected_artifact(owner, launch, installed_identity, profile):
        """Bind the owner archive identity separately from runtime profile digests."""
        role = installed_identity.source_role
        try:
            from .ios_mobile_native import prepared_app
            from .ios_mobile_inputs import IOSBaselineReference

            prepared = prepared_app(owner, role)
            payload = launch.payload
            app_digests = payload.get("appDigests")
            _require(type(app_digests) is dict
                     and app_digests.get(role) == prepared.app_digest
                     and installed_identity.source_app_digest == prepared.app_digest,
                     "ios_g4_identity", "Prepared app identity differs from launch")
            intent, _state = owner.operations._records(
                owner.operation.context.operation_id, owner._directory)
            archive = intent["roles"][role]
            reference = IOSBaselineReference(
                role, profile.bundle, owner.operation.archive_path(role),
                archive["sha256"], archive["bytes"])
            identity = reference.read()[1]
            artifact = profile.data["artifact"]
            expected_digest=archive['sha256'] if artifact['kind']=='ios-ipa' else identity['treeDigest']
            expected_bytes=archive['bytes'] if artifact['kind']=='ios-ipa' else identity['bytes']
            _require(expected_digest == artifact["sha256"]
                     and expected_bytes == artifact["bytes"]
                     and identity["bundleId"] == profile.bundle
                     and identity["bundleVersion"] == artifact["bundleVersion"]
                     and identity["bundleBuild"] == artifact["bundleBuild"],
                     "ios_g4_identity", "Selected archive differs from profile")
        except IOSG4Error:
            raise
        except Exception:
            raise IOSG4Error("ios_g4_identity",
                             "Selected archive identity is unavailable", 409) from None

    @property
    def network_window(self):
        """세션 경계에서 채취한 카운터 증거 쌍; 정책이 없으면 None."""
        if self._network_start is None and self._network_end is None:
            return None
        return {"start": deepcopy(self._network_start),
                "end": deepcopy(self._network_end)}

    @property
    def pump(self):
        return self._pump

    @property
    def owner_thread_id(self):
        return self._pump.owner_thread_id

    def bind_authority(self, device_authority, provider_incarnation):
        _require(device_authority is self.native_owner.device,
                 "ios_g4_authority", "Device authority is not the current owner device")
        expected = self.launch.payload.get("providerIncarnation")
        _require(type(provider_incarnation) is str and provider_incarnation == expected,
                 "ios_g4_authority", "Provider incarnation differs from XCTest launch")
        if self.device_authority is not None:
            _require(self.device_authority is device_authority
                     and self.provider_incarnation == provider_incarnation,
                     "ios_g4_authority", "Device authority binding changed")
            return
        self.device_authority = device_authority
        self.provider_incarnation = provider_incarnation

    def set_request_context(self, cancellation, deadline_monotonic):
        _bounds(cancellation, deadline_monotonic)
        self._context_cancellation = cancellation
        self._context_deadline = deadline_monotonic

    def _context(self):
        return self._context_cancellation, self._context_deadline

    def _require_authority(self, permit):
        _require(self.device_authority is not None
                 and self.provider_incarnation == getattr(permit, "provider_incarnation", None),
                 "ios_g4_authority", "Provider authority is unavailable")

    def _submit(self, kind, *, permit, session=None, lab=None, action=None, payload=None):
        cancellation, deadline = self._context()
        return self._pump.submit(
            kind, cancellation=cancellation, deadline_monotonic=deadline,
            permit=permit, session=session, lab=lab, action=action, payload=payload)

    def start_authorized(self, session, lab, permit):
        self._require_authority(permit)
        _require(type(session) is dict and type(lab) is Lab,
                 "ios_g4_start", "Lab session context is invalid")
        return self._submit(_RequestKind.START, permit=permit, session=session, lab=lab)

    def execute_authorized(self, action, payload, permit, frame=None):
        del frame
        self._require_authority(permit)
        _require(type(action) is str and action in self.profile.data["capabilities"]["actions"],
                 "unsupported_operation", "iOS G4 action is not declared", 400)
        _require(action in _ALLOWED_ACTIONS,
                 "unsupported_operation", "iOS G4 action is unsupported", 400)
        try:
            IOSHelperChannel.command_payload(action, payload)
            copied = deepcopy(payload)
        except Exception:
            raise IOSG4Error("invalid_argument", "iOS G4 action payload is invalid", 400) from None
        return self._submit(_RequestKind.EXECUTE, permit=permit,
                            action=action, payload=copied)

    def close_authorized(self, permit):
        self._require_authority(permit)
        if self._close_result is not None:
            _require(permit is self._close_permit, "ios_g4_authority", "Cleanup receipt belongs to another permit")
            return deepcopy(self._close_result)
        return self._submit(_RequestKind.CLOSE, permit=permit)

    def resolve_locator(self, *_args, **_kwargs):
        raise IOSG4Error("unsupported_operation",
                         "iOS G4 locator resolution is unsupported", 400)

    def observe(self, *_args, **_kwargs):
        raise IOSG4Error("unsupported_operation",
                         "iOS G4 semantic observation is unsupported", 400)

    def _check_native_bounds(self, request):
        _bounds(request.cancellation, request.deadline_monotonic)
        self._require_authority(request.permit)

    def _refresh_status(self, cancellation, deadline_monotonic, *, force=False):
        _bounds(cancellation, deadline_monotonic)
        helper = self._helper
        if helper is None:
            return None
        clock = self._clock
        needs_mapping = force or (clock is not None and clock.refresh_due())
        if clock is not None:
            sent = clock.synchronizer.sample()
        else:
            sent = received = None
        status = helper.status(cancellation=cancellation,
                               deadline_monotonic=deadline_monotonic)
        if clock is not None:
            received = clock.synchronizer.sample()
            if needs_mapping:
                clock.accept_status(helper.native_handshake, status, sent, received)
        return status

    def _publish_next_frame(self, cancellation, deadline_monotonic):
        _bounds(cancellation, deadline_monotonic)
        if self._helper is None or self._lab is None:
            return False
        frame = self._helper.frame_after(
            self._last_frame_id, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)
        if frame is None:
            return False
        require_runtime_frame(self.profile, frame["width"], frame["height"], frame["orientation"])
        try:
            data = base64.b64decode(frame["imageBase64"], validate=True)
        except Exception:
            raise IOSG4Error("invalid_frame", "iOS G4 native frame is invalid", 400) from None
        if self._clock is not None:
            timing = self._clock.frame_arguments(frame)
        else:
            timing = {"timing_source": "native-unmapped"}
        buffered = self._clock is not None and self._clock.buffered_frames
        gap = ((self._last_frame_id + 1, frame["nativeFrameId"] - 1)
               if (self._last_frame_id or buffered)
               and frame["nativeFrameId"] > self._last_frame_id + 1 else None)
        self._lab.publish_frame(
            self._sid, data, frame["mime"], frame["width"], frame["height"],
            frame["orientation"], frame["capturedAt"],
            acquisition_sequence=frame["nativeFrameId"], native_sequence_gap=gap,
            **timing)
        self._last_frame_id = frame["nativeFrameId"]
        return True

    def _publish_first_frame(self, cancellation, deadline_monotonic):
        while True:
            _bounds(cancellation, deadline_monotonic)
            if self._publish_next_frame(cancellation, deadline_monotonic):
                return
            time.sleep(min(0.01, max(0.001, deadline_monotonic - time.monotonic())))

    def _poll_owner(self, deadline_monotonic=None):
        if not self._started or self._closed or self._helper is None:
            return
        cancellation, context_deadline = self._context()
        deadline = context_deadline if deadline_monotonic is None else min(
            context_deadline, deadline_monotonic)
        _bounds(cancellation, deadline)
        self._refresh_status(cancellation, deadline)
        for _ in range(_FRAME_BATCH):
            if not self._publish_next_frame(cancellation, deadline):
                break

    def _owner_start(self, request):
        self._check_native_bounds(request)
        _require(not self._started and not self._closed,
                 "ios_g4_start", "iOS G4 provider was already started")
        self._lab, self._sid = request.lab, request.session["id"]
        cancellation, deadline = request.cancellation, request.deadline_monotonic
        try:
            self._session = self.runner.start(
                self.launch, permit=request.permit, cancellation=cancellation,
                deadline_monotonic=deadline)
            self._helper = IOSHelperChannel(self._session)
            self._handshake = self._helper.handshake(
                request.permit, cancellation=cancellation,
                deadline_monotonic=deadline)
            activation = self._helper.activate(
                request.permit, self.installed_identity,
                cancellation=cancellation, deadline_monotonic=deadline)
            if self.native_owner.operations.definition.egress_policy_digest is not None:
                start = activation.get("networkEvidence")
                _require(type(start) is dict,
                         "ios_g4_egress", "Egress counter baseline is unavailable")
                self._network_start = start
            reader = self.definition.open_runtime_reader(native_owner=self.native_owner)
            try:
                observation = reader.read(
                    self.launch, cancellation=cancellation,
                    deadline_monotonic=deadline)
                runtime = self.launch.payload.get("runtimeIdentity")
                runtime_keys={"bundleId", "buildId", "profileDigest", "runId"}
                if self.native_owner.operations.definition.sanitation_policy_digest is not None:
                    runtime_keys.add('sanitationPolicyDigest')
                if self.native_owner.operations.definition.egress_policy_digest is not None:
                    runtime_keys.add('egressPolicyDigest')
                _require(type(runtime) is dict
                         and set(runtime) == runtime_keys
                         and observation.bundle_id == runtime["bundleId"]
                         and observation.build_id == runtime["buildId"]
                         and observation.profile_digest == runtime["profileDigest"]
                         and observation.run_id == runtime["runId"],
                         "ios_g4_identity", "Runtime identity differs from launch marker")
                if runtime.get('sanitationPolicyDigest') is not None:
                    _require(observation.sanitation is not None and observation.sanitation.stage=='launch'
                        and observation.sanitation.policy_digest==runtime['sanitationPolicyDigest'],
                        'ios_g4_sanitation','App launch sanitation is unconfirmed')
            finally:
                _require(reader.close(deadline_monotonic=deadline),
                         "ios_g4_identity", "Runtime identity reader did not settle")
            self._clock = NativeFrameClock.for_session(
                request.lab, self._sid, self.device_authority)
            self._refresh_status(cancellation, deadline, force=True)
            self._publish_first_frame(cancellation, deadline)
            self._started = True
            return {"ok": True, "identityEvidence": observation.public()}
        except BaseException:
            self._emergency_close(cancellation, deadline)
            raise

    def _owner_execute(self, request):
        self._check_native_bounds(request)
        _require(self._started and not self._closed and self._helper is not None,
                 "session_inactive", "iOS G4 provider is not active")
        if request.action in {"launch", "terminate"}:
            raise IOSG4Error("unsupported_operation",
                             "iOS G4 relaunch is unsupported", 400)
        # The request lock is held only across the final abandonment check;
        # native helper I/O remains bounded and may observe cancellation.
        with self._pump._lock:
            _require(not request.abandoned,
                     "ios_g4_unknown", "iOS G4 action was abandoned", 409)
        result = self._helper.command(
            request.action, request.payload, request.permit,
            cancellation=request.cancellation,
            deadline_monotonic=request.deadline_monotonic)
        with self._pump._lock:
            _require(not request.abandoned,
                     "ios_g4_unknown", "iOS G4 action result is unknown", 409)
        self._refresh_status(request.cancellation, request.deadline_monotonic)
        for _ in range(_FRAME_BATCH):
            if not self._publish_next_frame(request.cancellation, request.deadline_monotonic):
                break
        return result

    def _owner_close(self, request):
        self._check_native_bounds(request)
        if self._close_result is not None:
            _require(request.permit is self._close_permit, "ios_g4_authority", "Cleanup receipt belongs to another permit")
            return deepcopy(self._close_result)
        self._close_permit = request.permit
        cancellation, deadline = request.cancellation, request.deadline_monotonic
        try:
            if self._helper is None:
                _require(self._session is None or self._session.close(deadline_monotonic=deadline),
                         "cleanup_uncertain", "XCTest host cleanup is unconfirmed")
                result = {"ok": True, "host": {"terminated": True}}
            else:
                sanitation=None
                if self.launch.payload['runtimeIdentity'].get('sanitationPolicyDigest') is not None:
                    cleanup=self._helper.command('cleanup',{},request.permit,
                        cancellation=cancellation,deadline_monotonic=deadline)
                    _require(cleanup.get('ok') is True,'cleanup_uncertain','App cleanup was not acknowledged')
                    reader=self.definition.open_runtime_reader(native_owner=self.native_owner)
                    try:
                        sanitation=reader.read(self.launch,stage='cleanup',cancellation=cancellation,
                            deadline_monotonic=deadline)
                    finally:
                        _require(reader.close(deadline_monotonic=deadline),'cleanup_uncertain','Cleanup reader remains active')
                result = self._helper.shutdown(
                    request.permit, cancellation=cancellation,
                    deadline_monotonic=deadline)
                if self.native_owner.operations.definition.egress_policy_digest is not None:
                    self._network_end = result.get('networkEvidence')
                    _require(type(self._network_end) is dict,
                             'ios_g4_egress', 'Egress counter end sample is unavailable')
                if sanitation is not None:result['sanitation']=sanitation.public()
            result = self._bounded_cleanup_result(result)
            if self._clock is not None:
                self._clock.close()
            self._closed = True
            self._started = False
            self._close_result = deepcopy(result)
            return deepcopy(result)
        except BaseException:
            result = self._emergency_close(cancellation, deadline)
            self._close_result = deepcopy(result)
            return result

    @staticmethod
    def _bounded_cleanup_result(result):
        """Require each process lifetime observation; this is not sanitation."""
        bounded = deepcopy(result) if isinstance(result, dict) else {
            "outcome": "unknown",
        }
        helper = bounded.get("helper")
        host = bounded.get("host")
        target = bounded.get("target")
        confirmed = (
            bounded.get("ok") is True
            and isinstance(helper, dict)
            and helper.get("terminationConfirmed") is True
            and isinstance(host, dict)
            and host.get("terminated") is True
            and host.get("terminationConfirmed", True) is True
            and isinstance(target, dict)
            and target.get("terminationConfirmed") is True
        )
        if not confirmed:
            bounded["ok"] = False
            bounded["outcome"] = "unknown"
            bounded["code"] = "cleanup_uncertain"
        return bounded

    def _emergency_close(self, cancellation, deadline):
        host = {"terminated": False, "terminationConfirmed": False}
        try:
            if self._session is not None:
                host["terminated"] = bool(
                    self._session.close(deadline_monotonic=deadline))
                host["terminationConfirmed"] = host["terminated"]
        except Exception:
            pass
        if self._runtime_reader is not None:
            try:
                self._runtime_reader.close(deadline_monotonic=deadline)
            except Exception:
                pass
            self._runtime_reader = None
        if self._clock is not None:
            try:
                self._clock.close()
            except Exception:
                pass
        self._closed = True
        self._started = False
        return {
            "ok": False,
            "outcome": "unknown",
            "code": "cleanup_uncertain",
            "host": host,
            "helper": {"terminationConfirmed": False},
            "target": {"terminationConfirmed": False},
        }

    def _execute_owner_request(self, request):
        if request.kind is _RequestKind.START:
            return self._owner_start(request)
        if request.kind is _RequestKind.EXECUTE:
            return self._owner_execute(request)
        if request.kind is _RequestKind.CLOSE:
            return self._owner_close(request)
        raise IOSG4Error("ios_g4_request", "Unknown iOS G4 request", 400)

    def run_replay(self, service: IssueSessionService, execution: ApprovedExecution, *,
                   registration: TrustedProjectRegistration, device_id, owner,
                   controller_id, preparations, issue_id, device_scope,
                   _candidate_identity, _candidate_profile, _startup_binding,
                   cancellation, deadline_monotonic):
        return _run_replay(
            self, service, execution, registration=registration,
            device_id=device_id, owner=owner, controller_id=controller_id,
            preparations=preparations, issue_id=issue_id,
            device_scope=device_scope, _candidate_identity=_candidate_identity,
            _candidate_profile=_candidate_profile,
            _startup_binding=_startup_binding, cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)


def _run_replay(provider, service, execution, *, registration, device_id, owner,
                controller_id, preparations, issue_id, device_scope,
                _candidate_identity, _candidate_profile, _startup_binding,
                cancellation, deadline_monotonic):
    _require(type(provider) is IOSG4Provider, "ios_g4_provider",
             "iOS G4 provider is invalid")
    _require(type(service) is IssueSessionService,
             "ios_g4_service", "Issue session service is required")
    _require(type(execution) is ApprovedExecution,
             "ios_g4_execution", "Issued approved execution is required")
    _require(type(registration) is TrustedProjectRegistration,
             "ios_g4_registration", "Trusted project registration is required")
    _bounds(cancellation, deadline_monotonic)
    _require(getattr(_startup_binding, "provider", None) is provider,
             "ios_g4_binding", "Startup binding does not name this provider")
    _require(getattr(_startup_binding, "scope", None) is device_scope,
             "ios_g4_binding", "Startup binding scope changed")
    cancellation=CallbackCancellation(cancellation)
    provider.set_request_context(cancellation, deadline_monotonic)
    result_box = {}

    def invoke():
        try:
            result_box["result"] = service.replay(
                execution, registration=registration, device_id=device_id,
                owner=owner, controller_id=controller_id,
                preparations=preparations, cancellation=cancellation,
                timeout_seconds=min(60.0, max(0.01,
                                              deadline_monotonic - time.monotonic())),
                issue_id=issue_id, device_scope=device_scope,
                _candidate_identity=_candidate_identity,
                _candidate_profile=_candidate_profile,
                _provider_factory=lambda: provider,
                _startup_binding=_startup_binding)
        except BaseException as error:
            result_box["error"] = error

    thread = threading.Thread(target=invoke, name="ios-g4-service", daemon=True)
    thread.start()

    def quarantine_unknown():
        provider.pump.abort()
        lab = provider._lab
        sid = provider._sid
        if lab is not None and sid is not None:
            try:
                lab.fail(sid, "iOS G4 owner work is unknown; device quarantined")
            except Exception:
                pass

    grace_deadline = deadline_monotonic
    abandonment_started = False
    while thread.is_alive() or provider.pump.pending:
        now = time.monotonic()
        if not abandonment_started and (now >= grace_deadline or cancellation.is_set()):
            abandonment_started = True
            cancellation.stopped.set()
            provider.pump.abandon_all(
                "deadline_exceeded" if now >= grace_deadline else "cancelled",
                "iOS G4 replay work was abandoned")
            grace_deadline = min(deadline_monotonic + 1.0, now + 1.0)
        if time.monotonic() >= grace_deadline and thread.is_alive():
            quarantine_unknown()
            raise IOSG4Error("ios_g4_unknown",
                             "iOS G4 replay did not settle", 409, unknown=True)
        try:
            provider.pump.pump_once(deadline_monotonic=grace_deadline)
        except BaseException as error:
            quarantine_unknown()
            cancellation.stopped.set()
            if thread.is_alive():
                thread.join(timeout=max(0, grace_deadline - time.monotonic()))
            raise IOSG4Error("ios_g4_unknown",
                             "iOS G4 owner work became unknown", 409,
                             unknown=True) from error
        if not provider.pump.pending:
            thread.join(timeout=0.005)
    thread.join(timeout=0)
    if provider.pump.fault is not None:
        raise IOSG4Error("ios_g4_unknown", "iOS G4 owner work became unknown",
                         409, unknown=True) from provider.pump.fault
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("result")


__all__ = ["IOSG4Error", "IOSOwnerCommandPump", "IOSG4Provider"]
