#!/usr/bin/env python3
"""Real shared-authority Android Live proof on a job-owned disposable emulator.

All UI input uses Lab -> AndroidLiveProvider -> the native :live helper. ADB is
used only for ownership checks and read-only fixture-state/cleanup evidence.
This is intentionally not a generic device test or an app instrumentation test.
"""
from __future__ import annotations

import argparse
import base64
import functools
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproof.android_profile import validate_android_runtime_profile
from reproof.core import digest
from reproof.device import AdbDevice
from reproof.live.android_live import AndroidLiveProvider, android_live_device
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from reproof.live.model import Lab, LiveError
from reproof.storage import sha_file, write_json


PACKAGE = "io.reproof.runtimefixture"
TARGETS = (
    "list_screen", "new_item", "row_notebook", "row_cable", "saved_item",
    "duplicate_open", "disabled_item", "form_screen", "item_name",
    "protected_entry", "save_item", "cancel_form", "validation_error",
    "detail_screen", "detail_title", "back_to_list", "missing_item",
)
SEED_ITEMS = ["Notebook", "Cable"]
CREATED_TITLE = "Runtime Widget"


class StartupDiagnostics:
    """Bounded, redacted, transparent observers, never alternate runtime logic.

    The actual provider and Lab types are unchanged. Each wrapper calls its
    original exactly once and preserves the exact return value or exception.
    No parameters, native bodies, error messages, locals, or config are saved.
    """
    ERROR_TYPES = frozenset({
        "LiveError", "ContractError", "DeviceError", "RuntimeError", "ValueError",
        "TimeoutError", "TimeoutExpired", "ConnectionRefusedError", "ConnectionResetError",
        "RemoteDisconnected", "OSError", "FileNotFoundError", "PermissionError",
    })
    ERROR_CODES = frozenset({
        "authority_required", "authority_unavailable", "authority_expired", "startup_rejected",
        "startup_unknown", "startup_timeout", "native_protocol_mismatch", "native_profile_mismatch",
        "native_timing_invalid", "native_bridge_error", "native_timeout", "helper_config_failed",
        "app_changed", "wrong_app", "app_identity", "install_failed", "forward_failed",
        "invalid_frame", "injection_unknown", "session_inactive", "cleanup_uncertain",
        "target_unavailable", "stale_geometry", "secure_input", "observation_failed",
    })
    REASONS = {
        "Clock mapping uncertainty is excessive": "clock_mapping_uncertainty_excessive",
        "Provider clock uncertainty is excessive": "provider_clock_uncertainty_excessive",
        "Native clock mapping is discontinuous": "native_clock_mapping_discontinuous",
        "Android helper handshake did not complete": "helper_handshake_incomplete",
        "Android helper did not become ready before target launch": "helper_not_ready_before_launch",
        "Android instrumentation stopped; close the session": "instrumentation_stopped",
        "Android stream disconnected; close and reconnect the device": "stream_disconnected",
        "Provider could not start": "provider_start_failed",
        "Provider cleanup failed": "provider_cleanup_failed",
        "Device release was not durably confirmed": "device_release_unconfirmed",
        "Device operation timed out": "adb_operation_timeout",
        "Device operation failed (check device authorization and sample installation)": "adb_operation_failed",
        "Native cleanup was not confirmed": "native_cleanup_unconfirmed",
        "Native session startup failed: failed": "native_session_startup_failed",
        "Native session startup failed: connecting": "native_session_startup_pending",
    }

    def __init__(self):
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.provider = None
        self.clock = None
        self.originals = []
        self.completed_phases = set()
        self.finished = False
        self.data = {
            "observer": "ci-harness-transparent-call-through-v1",
            "productionClassCodeUnchanged": True,
            "observerScope": "instance-call-through",
            "instanceMethodsObserved": ["provider.start_authorized", "provider._install",
                "provider._validate_native_metadata", "lab.fail", "frame_clock.accept_status"],
            "startupObserversRemoved": False,
            "events": [], "droppedEvents": 0, "failures": [], "labFailureReasons": [],
        }

    @classmethod
    def error(cls, error):
        name = type(error).__name__
        result = {"type": name if name in cls.ERROR_TYPES else "Exception"}
        code = getattr(error, "code", None)
        if isinstance(error, LiveError) and code in cls.ERROR_CODES:
            result["code"] = code
        # Exact fixed source messages can become enum values; unknown text is
        # deliberately not included, even when an exception embeds credentials.
        if len(error.args) == 1 and type(error.args[0]) is str:
            reason = cls.REASONS.get(error.args[0])
            if reason is not None:
                result["reason"] = reason
        trace = []
        current = error.__traceback__
        while current is not None:
            frame = current.tb_frame
            try:
                relative = Path(frame.f_code.co_filename).resolve().relative_to(ROOT).as_posix()
            except (ValueError, OSError):
                relative = ""
            if relative.startswith("reproof/") or relative == "scripts/android-runtime-fixture-qa.py":
                trace.append({"file": relative, "function": frame.f_code.co_name,
                              "line": current.tb_lineno})
            current = current.tb_next
        if trace:
            result["trace"] = trace[-8:]
        return result

    def event(self, phase, outcome, elapsed_ms=None):
        item = {"phase": phase, "outcome": outcome,
                "sinceStartMs": round((time.monotonic() - self.started) * 1000)}
        if elapsed_ms is not None:
            item["durationMs"] = elapsed_ms
        with self.lock:
            if len(self.data["events"]) < 64:
                self.data["events"].append(item)
            else:
                self.data["droppedEvents"] += 1

    def call(self, phase, original, *args, **kwargs):
        with self.lock:
            first = phase not in self.completed_phases
        if first:
            self.event(phase, "entered")
        started = time.monotonic()
        try:
            result = original(*args, **kwargs)
        except Exception as error:
            failure = dict(self.error(error), phase=phase)
            with self.lock:
                distinct = failure not in self.data["failures"]
                if distinct and len(self.data["failures"]) < 16:
                    self.data["failures"].append(failure)
                self.completed_phases.add(phase)
            if distinct:
                self.event(phase, "raised", round((time.monotonic() - started) * 1000))
            raise
        if first:
            self.event(phase, "returned", round((time.monotonic() - started) * 1000))
        with self.lock:
            self.completed_phases.add(phase)
        return result

    def observe_clock(self, clock):
        with self.lock:
            if self.finished or clock is None or clock is self.clock:
                return
            self.clock = clock
            original = clock.accept_status
            @functools.wraps(original)
            def observed(*args, **kwargs):
                return self.call("clock_accept_status", original, *args, **kwargs)
            self.originals.append((clock, "accept_status", original))
            clock.accept_status = observed

    def finish_startup(self):
        with self.lock:
            self.finished = True
            for instance, name, original in self.originals:
                setattr(instance, name, original)
            self.data["startupObserversRemoved"] = True

    def observe_provider(self, provider):
        require(type(provider) is AndroidLiveProvider, "Diagnostic observer requires the unchanged real provider type")
        self.provider = provider
        original_install = provider._install
        original_metadata = provider._validate_native_metadata
        original_start = provider.start_authorized
        self.originals.extend((provider, name, original) for name, original in (
            ("_install", original_install), ("_validate_native_metadata", original_metadata),
            ("start_authorized", original_start)))

        @functools.wraps(original_install)
        def install(apk, package, *args, **kwargs):
            phase = "helper_install" if package == "io.reproof.live" else "fixture_install"
            return self.call(phase, original_install, apk, package, *args, **kwargs)

        @functools.wraps(original_metadata)
        def metadata(value):
            if type(value) is dict:
                summary = {key: value.get(key) is True for key in ("ready", "stopped", "generalProfile")}
                summary.update(profileDigestMatches=value.get("profileDigest") == provider.app_profile.digest,
                               nativeDigestMatches=value.get("nativeDigest") == provider.app_profile.native_digest,
                               targetPackageMatches=value.get("targetPackage") == PACKAGE)
                for key in ("protocolVersion", "helperVersion"):
                    if type(value.get(key)) is int and 0 <= value[key] <= 100:
                        summary[key] = value[key]
                with self.lock:
                    self.data["lastNativeMetadata"] = summary
            result = self.call("native_metadata", original_metadata, value)
            self.observe_clock(provider.frame_clock)
            return result

        @functools.wraps(original_start)
        def start(*args, **kwargs):
            result = self.call("authorized_startup", original_start, *args, **kwargs)
            # No diagnostic wrappers remain on steady-state frame or input paths.
            self.finish_startup()
            return result

        provider._install, provider._validate_native_metadata = install, metadata
        provider.start_authorized = start
        return provider

    def observe_lab(self, lab):
        original = lab.fail
        @functools.wraps(original)
        def fail(sid, message):
            reason = self.REASONS.get(message, "unclassified_provider_failure") if type(message) is str else "unclassified_provider_failure"
            with self.lock:
                if len(self.data["labFailureReasons"]) < 8:
                    self.data["labFailureReasons"].append(reason)
            return original(sid, message)
        lab.fail = fail

    def snapshot(self, lab, session_id):
        state = {}
        if lab is not None and session_id in lab.sessions:
            session = lab.get_session(session_id)
            state.update(sessionState=session["state"],
                         sessionFailureReason=self.REASONS.get(session.get("error"), "none_or_unclassified"),
                         publishedFrames=session["metrics"]["frames"])
        provider = self.provider
        if provider is not None:
            state.update(nativeHandshakeBound=provider.native_handshake is not None,
                         handshakeReady=provider.handshake_ready is not None and provider.handshake_ready.is_set(),
                         helperReady=provider.helper_ready.is_set(), targetLaunched=provider.target_launched,
                         installedIdentityVerified=provider.installed_identity_evidence is not None,
                         nativeFramesReceived=provider.last_native_frame,
                         instrumentationStarted=provider.process is not None,
                         pollThreadAlive=provider.thread is not None and provider.thread.is_alive())
            if provider.process is not None:
                state["instrumentationReturnCode"] = provider.process.poll()
            clock = getattr(provider, "frame_clock", None)
            state["nativeClockMappingBound"] = clock is not None and clock._mapping is not None
            ack = getattr(provider, "last_ack", None)
            if type(ack) is dict:
                state["nativeAckSuccess"] = ack.get("ok") is True
                if ack.get("error") in self.ERROR_CODES:
                    state["nativeAckError"] = ack["error"]
        return state

    def report(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def environment_guard(serial, owned_name):
    """Fail before even discovering a device outside the explicitly owned CI job."""
    require(sys.platform.startswith("linux"), "Only the Linux CI emulator is supported")
    require(os.environ.get("CI") == "true"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("GITHUB_REPOSITORY") == "ictechgy/reproof"
            and os.environ.get("REPROOF_OWNED_ANDROID_EMULATOR") == "1",
            "The explicit GitHub-hosted owned-emulator sentinel is required")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[0-9]+", run_id) and re.fullmatch(r"[0-9]+", attempt)
            and owned_name == f"reproof-ci-{run_id}-{attempt}",
            "AVD ownership must match this exact GitHub run and attempt")
    require(re.fullmatch(r"emulator-[0-9]+", serial), "A named emulator serial is required")
    require(os.environ.get("ANDROID_SERIAL", serial) == serial,
            "ANDROID_SERIAL differs from the selected emulator")
    require(not any(os.environ.get(key) for key in (
        "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT",
        "ADB_SERVER_PORT")), "Remote or overridden ADB servers are not supported")


def preflight(args):
    environment_guard(args.serial, args.owned_emulator_name)
    app = args.app_apk.resolve()
    helper = args.helper_apk.resolve()
    require(app == ROOT / "android/runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk"
            and helper == ROOT / "android/live/build/outputs/apk/debug/live-debug.apk",
            "Only the two repository-built debug APK paths are accepted")
    require(app.is_file() and helper.is_file(), "Build both debug APKs before this proof")
    device = AdbDevice(args.serial)
    rows = [line.split() for line in device._command([device.adb, "devices"]).decode().splitlines()[1:]
            if line.strip()]
    require(rows == [[args.serial, "device"]], "The job must own the only ADB device, with no extra endpoints")
    require(device.shell("getprop", "ro.kernel.qemu").strip() == "1", "Selected device is not an emulator")
    actual_name = device.adb_call("emu", "avd", "name").decode().replace("\r", "").splitlines()
    require(actual_name == [args.owned_emulator_name, "OK"], "Connected AVD is not this job's owned AVD")
    require(device.shell("getprop", "sys.boot_completed").strip() == "1", "Owned emulator has not finished booting")
    require(device.apk_identity(app) == {"package": PACKAGE, "versionCode": 1}, "Fixture APK identity changed")
    require(device.apk_identity(helper) == {"package": "io.reproof.live", "versionCode": 2}, "Helper APK identity changed")
    sdk = Path(device.adb).resolve().parent.parent
    aapt = sorted((sdk / "build-tools").glob("*/aapt"), reverse=True)[0]
    permissions = device._command([str(aapt), "dump", "permissions", str(app)]).decode()
    require("uses-permission" not in permissions, "Offline fixture must not request any Android permissions")
    for package in (PACKAGE, "io.reproof.live"):
        installed = device.shell("pm", "list", "packages", package).splitlines()
        require(f"package:{package}" not in installed, "Fixture/helper already exists; use a fresh disposable AVD")
    require(not device.adb_call("forward", "--list").decode().strip(), "Fresh emulator proof requires no pre-existing ADB forwards")
    return device, app, helper


def runtime_profile(app, helper):
    source_files = [ROOT / "android/runtime-fixture/build.gradle.kts"]
    source_files += sorted((ROOT / "android/runtime-fixture/src").rglob("*"))
    sources = {path.relative_to(ROOT).as_posix(): sha_file(path)
               for path in source_files if path.is_file()}
    provenance = {"fixtureSources": sources, "appSha256": sha_file(app), "helperSha256": sha_file(helper)}
    profile = validate_android_runtime_profile({
        "schemaVersion": 2, "kind": "reproof-runtime-application",
        "id": "inventory_android", "projectId": "inventory_fixture",
        "projectDigest": digest(sources), "applicationId": "inventory_app",
        "buildId": "ci_debug", "platform": "android", "package": PACKAGE,
        "launchTarget": {"kind": "activity", "value": ".MainActivity"},
        "artifact": {"kind": "android-apk", "sha256": sha_file(app),
                     "bytes": app.stat().st_size, "provenanceDigest": digest(provenance), "versionCode": 1},
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {
            "actions": ["tap", "text", "launch"],
            "locator": {"kind": "android-resource-id", "version": 1, "targets": list(TARGETS)},
            "observations": ["pixels", "accessibility"],
            "geometry": {"maxWidth": 960, "maxHeight": 4096, "orientations": ["portrait"]},
            "captureAdapter": {"id": "native-frame", "version": 1}, "logAdapter": None,
        },
        "approvedReferences": {"launch": "launch_inventory", "preparations": []},
        "identityRequirement": "installed-sha256",
    })
    return profile, provenance


class RuntimeProof:
    def __init__(self, lab, session, device, output, result):
        self.lab, self.sid, self.device = lab, session["id"], device
        self.output, self.result = output, result
        self.owner = "ci-fixture-owner"
        self.provider = lab._session(self.sid)["provider"]
        require(type(self.provider) is AndroidLiveProvider and self.provider.general_profile,
                "This proof requires the real general AndroidLiveProvider")
        require(self.provider.device_authority is not None and self.provider.native_handshake is not None,
                "Shared-v2 authority and native handshake are required")
        self.launch_id = None

    def active(self):
        current = self.lab.get_session(self.sid, self.owner)
        require(current["state"] == "active", f"Runtime session is not active: {current['state']}")
        require(self.provider.device_authority.status == "owned", "Authority is not owned")
        return current

    def read_state(self):
        raw = self.device.adb_call("exec-out", "run-as", PACKAGE, "cat", "files/fixture-state.json")
        require(len(raw) <= 32 * 1024, "Fixture state exceeds its bound")
        value = json.loads(raw)
        require(value.get("schemaVersion") == 1 and type(value.get("revision")) is int,
                "Fixture oracle schema is invalid")
        if self.launch_id is None:
            self.launch_id = str(uuid.UUID(value["launchId"]))
        require(value["launchId"] == self.launch_id, "Fixture unexpectedly relaunched during proof")
        return value

    def locate(self, target):
        # Snapshot/frame races are read-only. Never repeat a mutation here.
        deadline = time.monotonic() + 8
        while True:
            self.active()
            try:
                found = self.lab.resolve_locator(self.sid, self.owner, {"kind": "resource-id", "value": target})
                self.result["locators"].append(found)
                return found
            except LiveError as error:
                if error.code != "observation_failed" or time.monotonic() >= deadline:
                    raise
                time.sleep(.1)

    def inject(self, action, payload, *, location=None, rejected_code=None):
        current = self.active()
        frame = location or self.lab.frame(self.sid, self.owner)
        command_id = "fixture_" + uuid.uuid4().hex
        command = {"controllerId": current["controllerId"], "epoch": current["epoch"],
                   "sequence": current["lastSequence"] + 1, "commandId": command_id,
                   "frameId": frame.get("frameId", frame.get("id")),
                   "geometryVersion": frame["geometryVersion"], "action": action, "payload": payload}
        try:
            receipt = self.lab.input(self.sid, self.owner, command)
        except LiveError as error:
            if rejected_code is None or error.code != "input_rejected":
                raise
            receipt = self.lab._session(self.sid)["receipts"][command_id][1]
            require(receipt["status"] == "rejected" and receipt["errorCode"] == rejected_code,
                    "Native rejection did not match the required fail-closed reason")
        else:
            require(rejected_code is None and receipt["status"] == "injected",
                    "A rejected operation was incorrectly reported as injected")
        self.active()
        require(self.provider.last_ack["ok"] is (rejected_code is None), "Native acknowledgement and Lab receipt differ")
        if rejected_code is not None:
            require(self.provider.last_ack["error"] == rejected_code, "Native rejection reason differs")
        self.result["receipts"].append(dict(receipt, nativeAck=dict(self.provider.last_ack)))
        return receipt

    def tap(self, target):
        found = self.locate(target)
        return self.inject("tap", {"x": found["x"], "y": found["y"]}, location=found)

    def checkpoint(self, name, screen, required_ids):
        deadline = time.monotonic() + 10
        while True:
            self.active()
            state = self.read_state()
            observed = self.provider.observe()
            nodes = observed.get("nodes", [])
            visible = [node["id"] for node in nodes if node.get("visible") is True]
            ready = (state["displayed"]["screen"] == screen and observed.get("ready") is True
                     and observed.get("locatorEvidence", {}).get("complete") is True
                     and all(visible.count(target) == 1 for target in required_ids))
            if ready:
                break
            require(time.monotonic() < deadline, f"Displayed/native UI did not reach checkpoint {name}")
            time.sleep(.1)
        # The visible checkpoint's unique marker must also resolve through Lab.
        self.locate(required_ids[0])
        # Wait for two newly published real frames after the native UI has been
        # observed. A previous screen's cached JPEG must not satisfy a checkpoint.
        observed_frame_id = self.lab.frame(self.sid, self.owner)["id"]
        deadline = time.monotonic() + 5
        while True:
            self.active()
            frame = self.lab.frame(self.sid, self.owner)
            if frame["id"] >= observed_frame_id + 2:
                break
            require(time.monotonic() < deadline, "Fresh native frames did not follow the displayed UI")
            time.sleep(.05)
        require(self.read_state() == state, "Displayed fixture changed during checkpoint capture")
        image = base64.b64decode(frame.pop("imageBase64"), validate=True)
        require(frame["mime"] == "image/jpeg" and image[:2] == b"\xff\xd8" and image[-2:] == b"\xff\xd9",
                "A real native JPEG frame is required")
        require(frame["width"] > 0 and frame["height"] > 0, "Native frame dimensions are unavailable")
        directory = self.output / "checkpoints"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.jpg").write_bytes(image)
        evidence = {"fixtureState": state, "nativeObservation": observed, "frame": frame,
                    "imageSha256": sha_file(directory / f"{name}.jpg")}
        write_json(directory / f"{name}.json", evidence)
        self.result["checkpoints"].append({"name": name, "screen": screen, "frameId": frame["id"],
                                           "revision": state["revision"], "imageSha256": evidence["imageSha256"]})
        return state, observed

    def reject_locator(self, target, expected_matches, *, disabled=False):
        before = self.read_state()
        sequence = self.active()["lastSequence"]
        observed = self.provider.observe()
        require(observed.get("ready") is True and observed.get("locatorEvidence", {}).get("complete") is True,
                "Negative locator evidence must be a complete native observation")
        matches = [node for node in observed["nodes"] if node["id"] == target]
        require(len(matches) == expected_matches, "Native negative-case shape is not what this test expects")
        if disabled:
            require(matches[0]["enabled"] is False, "Disabled target is not natively disabled")
        elif expected_matches:
            require(all(node["visible"] is True for node in matches), "Ambiguity requires two visible native nodes")
        try:
            self.lab.resolve_locator(self.sid, self.owner, {"kind": "resource-id", "value": target})
        except LiveError as error:
            require(error.code == "observation_failed", "Locator did not fail closed as an uncertain observation")
        else:
            raise RuntimeError(f"Unsafe locator unexpectedly resolved: {target}")
        require(self.active()["lastSequence"] == sequence and self.read_state() == before,
                "Negative locator case changed the app or dispatched an input")
        self.result["negativeCases"].append({"target": target, "nativeMatches": len(matches),
                                             "outcome": "observation_failed", "inputDispatched": False})

    def run(self):
        state, _ = self.checkpoint("initial-list", "list", ["list_screen", "new_item", "row_notebook", "row_cable"])
        require(state["model"]["items"] == SEED_ITEMS and state["displayed"]["items"] == SEED_ITEMS,
                "Initial model and displayed list differ")
        self.reject_locator("duplicate_open", 2)
        self.reject_locator("missing_item", 0)
        self.reject_locator("disabled_item", 1, disabled=True)
        self.tap("new_item")
        self.checkpoint("empty-form", "form", ["form_screen", "item_name", "protected_entry", "save_item"])
        self.tap("save_item")
        state, _ = self.checkpoint("invalid-form", "form", ["form_screen", "validation_error", "save_item"])
        require(state["event"] == "invalid_submit" and state["model"]["validationErrors"] == 1
                and state["model"]["items"] == SEED_ITEMS
                and state["displayed"]["validation"] == "Enter 1 to 40 characters",
                "Empty input was falsely accepted or no validation was displayed")
        self.tap("protected_entry")
        before = self.read_state()
        self.inject("text", {"value": "synthetic-only"}, rejected_code="secure_input")
        require(self.read_state() == before and before["displayed"]["protectedLength"] == 0,
                "Rejected protected input changed the fixture")
        self.result["negativeCases"].append({"target": "protected_entry", "outcome": "secure_input",
                                             "inputDispatched": True, "inputInjected": False})
        self.tap("item_name")
        self.inject("text", {"value": CREATED_TITLE})
        state, observed = self.checkpoint("filled-form", "form", ["form_screen", "item_name", "save_item"])
        require(state["displayed"]["draft"] == CREATED_TITLE
                and next(node for node in observed["nodes"] if node["id"] == "item_name").get("hasText") is True,
                "Real editable text did not reach the displayed input")
        self.tap("save_item")
        state, observed = self.checkpoint("created-list", "list", ["list_screen", "saved_item"])
        expected = SEED_ITEMS + [CREATED_TITLE]
        require(state["event"] == "created" and state["model"]["items"] == expected
                and state["displayed"]["items"] == expected
                and next(node for node in observed["nodes"] if node["id"] == "saved_item").get("hasText") is True,
                "Created model, displayed list, and native row differ")
        # Repeated navigation checks stable IDs and verifies no duplicate creation.
        for index in range(2):
            self.tap("saved_item")
            state, observed = self.checkpoint(f"detail-{index + 1}", "detail", ["detail_screen", "detail_title", "back_to_list"])
            require(state["model"]["selected"] == 2 and state["displayed"]["detailTitle"] == CREATED_TITLE
                    and next(node for node in observed["nodes"] if node["id"] == "detail_title").get("hasText") is True,
                    "Opened detail does not display the created item")
            self.tap("back_to_list")
            state, _ = self.checkpoint(f"returned-list-{index + 1}", "list", ["list_screen", "saved_item"])
            require(state["model"]["items"] == expected and state["displayed"]["items"] == expected,
                    "Back navigation changed the inventory")
        self.tap("new_item")
        self.checkpoint("cancel-form", "form", ["form_screen", "cancel_form"])
        self.tap("cancel_form")
        state, _ = self.checkpoint("cancelled-list", "list", ["list_screen", "saved_item"])
        require(state["event"] == "cancel_form" and state["model"]["items"] == expected
                and state["displayed"]["items"] == expected
                and state["model"]["duplicateActivations"] == 0
                and state["model"]["disabledActivations"] == 0,
                "Cancelled or negative-case actions changed inventory")
        self.result["invalidFormRejected"] = True
        self.result["createdItemShownInListAndDetail"] = True
        self.result["repeatedNavigationAndCancel"] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--owned-emulator-name", required=True)
    parser.add_argument("--helper-apk", type=Path, required=True)
    parser.add_argument("--app-apk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(not args.output.exists(), "Use a new proof output directory")
    args.output.mkdir(parents=True, mode=0o700)
    result = {"schemaVersion": 1, "passed": False, "provider": "AndroidLiveProvider",
              "profileMode": "general", "authorityMode": "shared-v2",
              "checkpoints": [], "receipts": [], "locators": [], "negativeCases": [],
              "cleanup": {"confirmed": False}}
    lab = authority = device = None
    session_id = None
    diagnostics = StartupDiagnostics()
    with tempfile.TemporaryDirectory(prefix="reproof-fixture-authority-") as directory:
        try:
            device, app, helper = preflight(args)
            result["ownership"] = {"serial": args.serial, "avd": args.owned_emulator_name,
                                   "githubRunId": os.environ["GITHUB_RUN_ID"], "qemu": True}
            profile, provenance = runtime_profile(app, helper)
            write_json(args.output / "runtime-profile.json", profile.data)
            write_json(args.output / "build-provenance.json", provenance)
            private = Path(directory)
            authority = HostAuthority(private / "authority.sqlite3", lease_directory=private / "leases")
            grant = issue_local_parent_grant(authority, lifetime_ns=600_000_000_000)
            descriptor = android_live_device(args.serial, helper, app, runtime_profile=profile)
            original_factory = descriptor["factory"]
            def observed_factory():
                provider = diagnostics.call("provider_construction", original_factory)
                return diagnostics.observe_provider(provider)
            descriptor["factory"] = observed_factory
            lab = Lab([descriptor], private / "live", authority=authority, parent_grant=grant,
                      idle_timeout=120, max_session_seconds=600)
            diagnostics.observe_lab(lab)
            session = lab.create_session(descriptor["id"], "ci-fixture-owner", "ci-fixture-controller")
            session_id = session["id"]
            deadline = time.monotonic() + 30
            while session["state"] == "connecting" and time.monotonic() < deadline:
                time.sleep(.1)
                session = lab.get_session(session_id)
            require(session["state"] == "active", f"Native session startup failed: {session['state']}")
            proof = RuntimeProof(lab, session, device, args.output, result)
            result["identityEvidence"] = proof.provider.installed_identity_evidence
            handshake = proof.provider.native_handshake
            result["nativeHandshake"] = {"protocolVersion": handshake.protocol_version,
                                         "helperVersion": handshake.helper_version,
                                         "nativeClockId": handshake.native_clock_id,
                                         "nativeIncarnation": handshake.native_incarnation}
            require(result["identityEvidence"]["installedArtifactDigest"] == sha_file(app)
                    and handshake.protocol_version == 2 and handshake.helper_version == 2,
                    "Installed digest or native authority protocol differs")
            proof.run()
            result["runtimeAssertionsPassed"] = True
        except Exception as error:
            result["failure"] = diagnostics.error(error)
        finally:
            observed_sid = session_id if session_id is not None else next(iter(lab.sessions), None) if lab is not None else None
            try:
                result["beforeCleanup"] = diagnostics.snapshot(lab, observed_sid)
            except Exception as error:
                result["beforeCleanup"] = {"snapshotFailure": diagnostics.error(error)}
            cleanup_errors = []
            try:
                if lab is not None:
                    # Include an admitted session even if create_session raised.
                    ids = [session_id] if session_id is not None else list(lab.sessions)
                    closed = [lab.close_session(sid, "ci-fixture-owner") for sid in ids]
                    require(bool(closed) and all(item["state"] == "closed" for item in closed),
                            "Native cleanup was not confirmed")
                    require(all(item["state"] == "available" for item in lab.devices.values()),
                            "Device authority remains quarantined or held")
                    require(not device.adb_call("forward", "--list").decode().strip(),
                            "ADB forward remained after provider cleanup")
                    result["cleanup"] = {"confirmed": True, "sessionStates": [item["state"] for item in closed],
                                         "authorityReleased": True, "adbForwardsRemaining": 0}
            except Exception as error:
                cleanup_errors.append(diagnostics.error(error))
            finally:
                # Closing the host authority must still be attempted after a
                # native cleanup failure. It does not turn quarantine into a
                # successful device release or retry the uncertain UI action.
                if authority is not None:
                    try:
                        authority.close()
                    except Exception as error:
                        cleanup_errors.append(dict(diagnostics.error(error), phase="authority_close"))
            if cleanup_errors:
                result["cleanup"] = {"confirmed": False, "errors": cleanup_errors}
            try:
                result["afterCleanup"] = diagnostics.snapshot(lab, observed_sid)
            except Exception as error:
                result["afterCleanup"] = {"snapshotFailure": diagnostics.error(error)}
            result["startupDiagnostics"] = diagnostics.report()
            result["passed"] = (result.get("runtimeAssertionsPassed") is True
                                and result["cleanup"]["confirmed"] is True and "failure" not in result)
            write_json(args.output / "result.json", result)
    print(json.dumps({"passed": result["passed"], "checkpoints": len(result["checkpoints"]),
                      "nativeInputs": len(result["receipts"]), "cleanup": result["cleanup"],
                      "failure": result.get("failure")}, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
