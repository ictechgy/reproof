#!/usr/bin/env python3
"""One original UIKit flow, nine inputs, eight one-shot OCR checks at most.

Diagnostic only: unchanged original source, observer, oracles and action block.
No retries, offline follow-on, production patch, or broad acceptance claim.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-18-5"
DEVICE = "com.apple.CoreSimulator.SimDeviceType.iPhone-16"
OWNER = "ci-original-flow-owner"
DEVELOPER_DIR = "/Applications/Xcode_16.4.app/Contents/Developer"
MAX_REPORT_BYTES = 524288
MAX_OCR_CALLS = 8
MAX_INPUTS = 9

class DiagnosticError(RuntimeError):
    pass

def require(value, code):
    if not value:
        raise DiagnosticError(code)

PINS = {'scripts/ios-native-fixture-qa.py': 'c4babe0acd04d877c5202c03bc8692bb6b0bcac75ddb0c7bef9ff14be225a6e5', 'ios-native-fixture/App/AppDelegate.swift': 'f345119262f18fcf0d9d7993c762a58680aa1fa613b641e6e9fdf935ae3df675', 'ios-native-fixture/App/Info.plist': '9080e5c27b7638957afd01c47926bbdaece6f5a176dc702c33d06630303c0f57', 'ios-native-fixture/ObserveFrame.swift': '18ad97694079c779f39111b0f31d0ab6336e5644d61860263c604503c28573c9', 'ios-native-fixture/observation-profile.json': '11e1a07b6e7a666bd985011011d3ed735cd991cb0435f6cfe4e9f30f1ac35e73', 'ios-native-fixture/InventoryFixture.xcodeproj/project.pbxproj': 'a1b90689e38f16f3e1cfdda38ec9de137152c03013842d56bf9261a0fea3ab35', 'reproof/live/model.py': '2e9abf6a746e91d56813bc25a8c16af6195b5e38a43a28b4eb627d258a365175', 'reproof/live/providers.py': '2a6497c06285c6e4999bd57a56de73deaf5d3b21a0415c4bb2f74d40a05dfed8', 'live-ios/Tests/LiveControlTests.swift': '408f86c5694ba3f6e260dee00b1ddd320888d3ce7118f9b4423c5cfc4acd8c46'}

def validate_inputs():
    for name, expected in PINS.items():
        path = ROOT / name
        require(path.is_file() and not path.is_symlink()
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "pinned_source_mismatch")

validate_inputs()
spec = importlib.util.spec_from_file_location("original_flow_native", ROOT / "scripts/ios-native-fixture-qa.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def command(argv, *, timeout=30):
    try:
        return native.command(argv, timeout=timeout)
    except Exception:
        raise DiagnosticError("native_command_failed") from None


def verify_host():
    validate_inputs()
    require(platform.system() == "Darwin", "github_macos_host_required")
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS",
        "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_REPOSITORY": "ictechgy/reproof",
        "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "1"}
    require(all(os.environ.get(k) == v for k, v in expected.items()), "first_hosted_pr_run_required")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    require(event["action"] == "opened" and event["repository"]["private"] is False
            and event["repository"]["full_name"] == "ictechgy/reproof"
            and event["pull_request"]["head"]["repo"]["full_name"] == "ictechgy/reproof"
            and event["pull_request"]["base"]["ref"] == "codex/owned-native-runtime-acceptance",
            "approved_opened_event_required")
    require(os.environ.get("DEVELOPER_DIR") == DEVELOPER_DIR and Path(DEVELOPER_DIR).is_dir(),
            "pinned_xcode_unavailable")
    require(command(["/usr/bin/xcodebuild", "-version"], timeout=10).decode().strip()
            == "Xcode 16.4\nBuild version 16F6", "pinned_xcode_version_mismatch")
    version = command(["/usr/bin/sw_vers", "-productVersion"], timeout=10).decode().strip()
    require(re.fullmatch(r"15(?:\.[0-9]{1,2}){1,2}", version), "pinned_macos_version_mismatch")
    return {"macOS": version, "xcode": "16.4", "xcodeBuild": "16F6"}


def simulator_state(simulator, name, expected=None):
    require(re.fullmatch(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}", simulator),
            "explicit_simulator_uuid_required")
    document = json.loads(command(["/usr/bin/xcrun", "simctl", "list", "devices", "--json"]))
    matches = [(runtime, device) for runtime, devices in document["devices"].items()
               for device in devices if device.get("udid") == simulator]
    require(len(matches) == 1, "owned_simulator_not_unique")
    runtime, device = matches[0]
    require(runtime == RUNTIME and device.get("name") == name and device.get("isAvailable") is True
            and device.get("deviceTypeIdentifier") == DEVICE, "owned_simulator_identity_mismatch")
    state = device.get("state")
    require(state in {"Booted", "Shutdown"}, "owned_simulator_state_unknown")
    if expected is not None:
        require(state == expected, "owned_simulator_state_mismatch")
    return {"uuid": simulator, "name": name, "runtime": runtime, "deviceType": DEVICE, "state": state}


def shutdown_simulator(simulator, name):
    # Revalidate exact ownership before mutation; never shutdown/delete all.
    before = simulator_state(simulator, name)
    if before["state"] != "Shutdown":
        command(["/usr/bin/xcrun", "simctl", "shutdown", simulator], timeout=30)
    return simulator_state(simulator, name, "Shutdown")


class OwnedCapture:
    def __init__(self, args, build, work):
        self.args, self.build, self.work = args, build, work
        self.lab = self.server = self.thread = self.authority = self.session = self.provider = None
        self.handshake = None
        self.thread_started = False
        self.cleanup_attempted = False
        self.health_failure = None
        self.cleanup = {"confirmed": False, "state": "not_started"}

    def start(self):
        native.verify_ci_simulator(self.args.simulator, self.args.owned_simulator_name)
        simulator_state(self.args.simulator, self.args.owned_simulator_name, "Booted")
        profile = native.runtime_profile(self.build, label="original", logs=False)
        descriptor = native.ios_device(self.args.simulator, self.args.helper_products, profile.bundle,
                                       app=self.build["app"], profile=profile)
        require(descriptor["capabilities"]["locatorKinds"] == [], "semantic_capability_must_be_absent")
        self.descriptor = descriptor
        self.authority = native.HostAuthority(self.work / "authority.sqlite3", lease_directory=self.work / "leases")
        grant = native.issue_local_parent_grant(self.authority, lifetime_ns=600_000_000_000)
        self.lab = native.Lab([descriptor], self.work / "live", authority=self.authority, parent_grant=grant,
                              idle_timeout=120, max_session_seconds=600)
        self.server = native.LiveServer(self.lab, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.thread_started = True
        self.session = self.lab.create_session(descriptor["id"], OWNER, "ci-controller")
        deadline = time.monotonic() + 95
        while self.session["state"] == "connecting" and time.monotonic() < deadline:
            time.sleep(0.2)
            self.session = self.lab.get_session(self.session["id"], OWNER)
        require(self.session["state"] == "active", "native_provider_not_active")
        self.provider = self.lab.sessions[self.session["id"]]["provider"]
        self.handshake = self.provider.native_handshake
        require(type(self.provider) is native.IosProvider, "exact_ios_provider_required")
        require(self.handshake is not None and self.provider.identity == {
            "bundle": profile.bundle, "artifactDigest": self.build["receipt"]["artifactDigest"]},
            "installed_identity_or_handshake_unconfirmed")
        return {"bundle": profile.bundle, "selectedArtifactSha256": self.build["receipt"]["artifactDigest"],
                "installedArtifactSha256": self.provider.identity["artifactDigest"],
                "nativeHandshake": True, "authority": "existing-native-v2-protocol",
                "screenScope": "original offline UIKit fixture full gesture flow"}

    def active(self):
        session = self.lab.sessions[self.session["id"]]
        provider = self.provider
        return (session["state"] == "active" and not session.get("stopAdmission", False)
                  and session["provider"] is provider and provider.native_handshake is self.handshake
                  and provider.handshake_ready is not None and provider.handshake_ready.is_set()
                  and not provider.stop.is_set() and provider.process is not None
                  and provider.process.poll() is None and provider.udid == self.args.simulator
                  and provider.identity["artifactDigest"] == self.build["receipt"]["artifactDigest"])

    def assert_healthy(self):
        require(self.health_failure is None, self.health_failure or "capture_health_failed")
        require(self.active(), "capture_inactive")

    def sample(self):
        session = self.lab.sessions[self.session["id"]]
        active = self.active()
        frame_lock = session["frameLock"]
        require(frame_lock.acquire(timeout=0.2), "capture_frame_lock_timeout")
        try:
            frame = session["frame"]
            require(frame is not None, "capture_frame_missing")
            require(session["frameSequence"] == frame["id"], "capture_sequence_unbound")
            return {"active": active, "frameId": frame["id"], "received": frame["_received"],
                    "frameBytes": len(frame["bytes"]), "width": frame["width"], "height": frame["height"],
                    "mime": frame["mime"], "history": list(session["frameHistory"])}
        finally:
            frame_lock.release()

    def close(self):
        if self.cleanup_attempted:
            return self.cleanup
        self.cleanup_attempted = True
        self.cleanup = {"confirmed": False, "state": "not_created"}
        if self.session is not None:
            try:
                # Bridge must remain live until authenticated exact-target cleanup returns.
                closed = self.lab.close_session(self.session["id"], OWNER, require_finished_inputs=True)
                state = self.lab.devices[self.descriptor["id"]]["state"]
                provider = self.provider or self.lab.sessions[self.session["id"]].get("provider")
                provider_clean = (provider is not None and provider.stop.is_set()
                                  and provider.process is not None and provider.process.poll() == 0
                                  and provider.temp is None and provider.lease is None)
                self.cleanup = {"confirmed": closed["state"] == "closed" and state == "available" and provider_clean,
                                "state": closed["state"], "deviceState": state,
                                "providerCleanupConfirmed": provider_clean}
            except Exception:
                self.cleanup = {"confirmed": False, "state": "cleanup_failed"}
        # No close_all/recover: do not retry failed cleanup or release unproven ownership.
        operations = []
        if self.server is not None:
            operations = [self.server.close_operations]
            if self.thread_started:
                operations.append(self.server.shutdown)
            operations.append(self.server.server_close)
        for operation in operations:
            try:
                operation()
            except Exception:
                self.cleanup.update(confirmed=False, serverCleanupFailed=True)
        if self.thread_started:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                self.cleanup.update(confirmed=False, serverThreadStillRunning=True)
        if self.lab is not None:
            try:
                self.lab.stop_maintenance()
                self.lab.close_evidence_store()
            except Exception:
                self.cleanup.update(confirmed=False, storeCleanupFailed=True)
        # Preserve failed authority/quarantine journals privately on this disposable host.
        if self.authority is not None and (self.cleanup["confirmed"] or self.lab is None):
            try:
                self.authority.close()
            except Exception:
                self.cleanup.update(confirmed=False, authorityCleanupFailed=True)
        self.cleanup["privateStateRetained"] = not self.cleanup["confirmed"]
        return self.cleanup


OBSERVER_PROCESS_TIMEOUT_SECONDS = native.OBSERVER_PROCESS_TIMEOUT_SECONDS
PIXEL_STATE_TIMEOUT_SECONDS = native.PIXEL_STATE_TIMEOUT_SECONDS
FIXTURE_REVISION = native.FIXTURE_REVISION
PIXEL_ORACLE_REVISION = native.PIXEL_ORACLE_REVISION
LiveError = native.LiveError
GateFailure = native.GateFailure
save = native.save
normalized_words = native.normalized_words
state_matches = native.state_matches
observe_frame = native.observe_frame

def observe_state(frame_source, observer, output, proof, name, required, forbidden=(), *, last_frame=0, health=None):
    """One absolute state budget, one OCR call per state, no mismatch retry."""
    require(len(proof.get("observationDiagnostics", [])) < MAX_OCR_CALLS, "observer_call_limit")
    started = time.monotonic()
    deadline = started + PIXEL_STATE_TIMEOUT_SECONDS
    state = {"name": name, "outcome": "observing", "attempts": []}
    proof.setdefault("observationDiagnostics", []).append(state)
    attempt = None

    def remaining(phase):
        state["phase"] = phase
        left = deadline - time.monotonic()
        if left <= 0:
            state["outcome"] = "deadline_exceeded"
            if attempt is not None and attempt["outcome"] == "observing":
                attempt["outcome"] = "deadline_exceeded"
            raise GateFailure("pixel_state_deadline_exceeded_" + name)
        return left

    def pause():
        time.sleep(min(0.2, remaining("poll_wait")))
        remaining("poll_wait")

    try:
        while True:
            remaining("frame_request")
            if health is not None:
                health()
                remaining("health_before_frame")
            try:
                frame = frame_source()
            except LiveError as error:
                remaining("frame_received")
                if error.code != "frame_pending":
                    raise
                pause()
                continue
            remaining("frame_received")
            if frame["id"] <= last_frame:
                pause()
                continue
            last_frame = frame["id"]
            number = len(state["attempts"]) + 1
            stem = f"{name}-attempt-{number:03d}"
            attempt = {"attempt": number, "frameId": frame["id"],
                       "geometryVersion": frame["geometryVersion"], "outcome": "observing"}
            state["attempts"].append(attempt)
            require(type(frame["imageBase64"]) is str and len(frame["imageBase64"]) <= 4 * 1024 * 1024,
                    "frame_size_limit")
            image = base64.b64decode(frame["imageBase64"], validate=True)
            image_sha = hashlib.sha256(image).hexdigest()
            attempt["imageSha256"] = image_sha
            remaining("image_decoded")
            path = output / (stem + ".jpg")
            # The output directory is new for this flow; exclusive creation
            # prevents a repeated state name from overwriting earlier evidence.
            with path.open("xb") as stream:
                stream.write(image)
            attempt["imageFile"] = path.name
            remaining("image_written")
            if health is not None:
                health()
                remaining("health_before_observer")
            timeout = min(OBSERVER_PROCESS_TIMEOUT_SECONDS, remaining("observer_start"))
            attempt["processTimeoutSeconds"] = timeout
            proof["observerCalls"] = proof.get("observerCalls", 0) + 1
            attempt["processStartSinceStateMs"] = round((time.monotonic()-started)*1000)
            try:
                raw = observe_frame(observer, path, timeout, attempt)
            except GateFailure:
                remaining("observer_returned")
                raise
            finally:
                attempt["processEndSinceStateMs"] = round((time.monotonic()-started)*1000)
            remaining("observer_returned")
            if health is not None:
                health()
                remaining("health_after_observer")
            try:
                require(len(raw) <= 65536, "observer_output_limit")
                actual = json.loads(raw)
            except (ValueError, UnicodeError):
                raise GateFailure("pixel_observer_invalid") from None
            remaining("observer_parsed")
            normalized_words(actual)
            require(len(actual["observations"]) <= 128
                    and all(len(row["text"]) <= 512 for row in actual["observations"]), "observer_output_limit")
            remaining("observer_validated")
            ocr_path = output / (stem + "-ocr.json")
            # Save only the validated, bound result, not unrecognized output fields.
            save(ocr_path, {"frameId": frame["id"], "imageSha256": image_sha,
                            "observations": actual["observations"]})
            attempt["ocrFile"] = ocr_path.name
            remaining("ocr_written")
            matches = state_matches(actual, required, forbidden)
            remaining("required_oracle")
            if matches:
                # Prove the very same oracle cannot silently accept a wrong state.
                false_positive = state_matches(actual, ["IMPOSSIBLE FIXTURE STATE"], [])
                remaining("negative_oracle")
                require(not false_positive, "pixel_oracle_false_positive")
                accepted = {"name": name, "frameId": frame["id"],
                    "geometryVersion": frame["geometryVersion"], "imageSha256": image_sha,
                    "imageFile": path.name, "ocrFile": ocr_path.name,
                    "required": list(required), "forbidden": list(forbidden), "passed": True,
                    "fixtureRevision": FIXTURE_REVISION, "pixelOracleRevision": PIXEL_ORACLE_REVISION}
                if health is not None:
                    health()
                remaining("before_pass")
                attempt["outcome"] = state["outcome"] = "passed"
                proof["states"].append(accepted)
                return last_frame
            attempt["outcome"] = state["outcome"] = "mismatch"
            raise DiagnosticError("pixel_state_mismatch_" + name)
    except Exception:
        if state["outcome"] == "observing":
            state["outcome"] = {"timeout": "observer_timeout",
                                "deadline_exceeded": "observer_deadline_exceeded"}.get(
                                    attempt.get("processOutcome") if attempt else None, "failed")
        if attempt is not None and attempt["outcome"] == "observing":
            attempt["outcome"] = state["outcome"]
        raise
    finally:
        state["elapsedMs"] = max(0, round((time.monotonic() - started) * 1000))


def perform_flow(capture, observer, output, proof):
    lab, sid = capture.lab, capture.session["id"]
    owner, controller = OWNER, "ci-controller"
    session = capture.session
    last_frame = 0
    sequence = 0
    flow_started = time.monotonic()

    def observe(name, required, forbidden=()):
        nonlocal last_frame
        started = time.monotonic()
        try:
            last_frame = observe_state(lambda: lab.frame(sid, owner), observer, output, proof,
                                       name, required, forbidden, last_frame=last_frame, health=capture.assert_healthy)
        finally:
            if proof.get("observationDiagnostics"):
                proof["observationDiagnostics"][-1]["startSinceFlowMs"] = round((started-flow_started)*1000)

    def send(action, payload):
        nonlocal sequence, last_frame
        require(sequence < MAX_INPUTS, "input_limit")
        capture.assert_healthy()
        frame = lab.frame(sid, owner)
        sequence += 1
        last_frame = max(last_frame, frame["id"])
        proof["inputsAttempted"] = sequence
        receipt = lab.input(sid, owner, {"controllerId": controller, "epoch": session["epoch"],
            "sequence": sequence, "commandId": uuid.uuid4().hex,
            "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
            "action": action, "payload": payload})
        require(receipt["status"] == "injected", "input_not_confirmed")
        proof["receipts"].append({"sequence": sequence, "status": receipt["status"], "action": action})

    def tap(y):
        send("tap", {"x": 0.5, "y": y})

    observe("01-empty", ["INVENTORY LIST", "ITEM TOTAL ZERO", "INVENTORY IS EMPTY"], ["BLUE MUG"])
    tap(0.34)
    observe("02-editor", ["CREATE ITEM", "ITEM TOTAL ZERO", "SAVE ITEM"])
    tap(0.44)
    observe("03-invalid", ["CREATE ITEM", "ITEM TOTAL ZERO", "NAME REQUIRED"], ["ITEM TOTAL ONE"])
    tap(0.32)
    send("text", {"value": "BLUE MUG"})
    tap(0.44)
    observe("04-created", ["INVENTORY LIST", "ITEM TOTAL ONE", "BLUE MUG"], ["INVENTORY IS EMPTY", "NAME REQUIRED"])
    tap(0.49)
    observe("05-detail", ["ITEM DETAILS", "BLUE MUG", "BACK TO LIST"])
    tap(0.50)
    observe("06-back", ["INVENTORY LIST", "ITEM TOTAL ONE", "BLUE MUG"])
    tap(0.34)
    observe("07-cancel-editor", ["CREATE ITEM", "ITEM TOTAL ONE"])
    tap(0.54)
    observe("08-cancelled", ["INVENTORY LIST", "ITEM TOTAL ONE", "BLUE MUG"], ["ITEM TOTAL ZERO"])
    require(sequence == MAX_INPUTS and len(proof["states"]) == MAX_OCR_CALLS, "flow_incomplete")
    capture.assert_healthy()
    proof["flowVerified"] = True

class HealthMonitor:
    """Read-only bounded publication/health sampling, never a readiness delay.

Samples are diagnostic, not an oracle or proof of continuous activity. No native
commands, frame OCR, UI inputs, or cleanup operations originate here.
"""
    def __init__(self, capture):
        self.capture = capture
        self.started = time.monotonic()
        self.rows = []
        self.stop = threading.Event()
        self.thread = None
        self.failure = None

    def start(self):
        def poll():
            while not self.stop.is_set():
                if len(self.rows) >= 1200 or time.monotonic() - self.started > 300:
                    self.failure = "sampling_bound_reached"
                    return
                try:
                    data = self.capture.sample()
                    if not data["active"]:
                        self.capture.health_failure = "capture_inactive"
                    now = time.monotonic()
                    # Never publish raw frames, process argv, identity, or private paths.
                    self.rows.append({"atMs": round((now-self.started)*1000),
                        "active": data["active"], "frameId": data["frameId"],
                        "frameBytes": data["frameBytes"], "width": data["width"], "height": data["height"],
                        "frameAgeMs": max(0, round((now-data["received"])*1000)),
                        "historyFirstFrameId": data["history"][0][0] if data["history"] else None,
                        "historyLastFrameId": data["history"][-1][0] if data["history"] else None})
                except Exception as error:
                    # Initial frame_pending is expected; all other unknown health fails closed.
                    if not isinstance(error, DiagnosticError) or str(error) != "capture_frame_missing":
                        self.capture.health_failure = "capture_health_unknown"
                    self.rows.append({"atMs": round((time.monotonic()-self.started)*1000),
                                      "sampleUnavailable": True})
                self.stop.wait(0.25)
        self.thread = threading.Thread(target=poll, name="original-flow-health", daemon=True)
        self.thread.start()

    def finish(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.failure = "monitor_stop_unconfirmed"
        return {"samplePeriodMs": 250, "maxSamples": 1200, "failure": self.failure,
                "samples": list(self.rows)}


def new_report():
    return {"schemaVersion": 1, "kind": "original-uikit-one-shot-flow", "passed": False,
            "nativeAcceptance": False, "baseline": "6eb207897f5819bf74b949c062a88e67df810abd",
            "sourceHashes": PINS, "observerProcessTimeoutSeconds": 15, "stateTimeoutSeconds": 25,
            "maximumOCRCalls": MAX_OCR_CALLS, "maximumInputs": MAX_INPUTS,
            "inputsAttempted": 0, "observerCalls": 0, "states": [], "receipts": [],
            "outcome": "not_started", "cleanup": {"confirmed": False, "state": "not_started"},
            "limits": ["One original UIKit fixture flow, not company app or physical device acceptance",
                       "No repeatability or causation claim from one attempt",
                       "Sampled health metadata is not proof of continuous activity"]}


def execute(args, proof):
    proof["host"] = verify_host()
    proof["ownership"] = native.verify_ci_simulator(args.simulator, args.owned_simulator_name)
    simulator_state(args.simulator, args.owned_simulator_name, "Booted")
    require(args.helper_products.is_dir(), "built_helper_products_required")
    work = Path(tempfile.mkdtemp(prefix="original-flow-private-")).resolve(strict=True)
    capture = monitor = None
    try:
        proof["helperPreflight"] = native.helper_preflight(args.helper_products, work)
        source = ROOT / "ios-native-fixture"
        observer = work / "observe-frame"
        command(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision", "-framework", "Foundation",
                 str(source / "ObserveFrame.swift"), "-o", str(observer)], timeout=90)
        profile = native.load_observation_profile(source / "observation-profile.json")
        build = native.build_observation_app(source, work / "original-build", profile=profile)
        receipt = build["receipt"]
        require(receipt["signed"] is False and receipt["automaticObservations"] is False,
                "original_unsigned_fixture_required")
        proof["build"] = {key: receipt[key] for key in ("sourceDigest", "artifactDigest", "artifactBytes",
                           "applicationId", "signed", "automaticObservations")}
        capture = OwnedCapture(args, build, work)
        proof["capture"] = capture.start()
        monitor = HealthMonitor(capture)
        monitor.start()
        perform_flow(capture, observer, args.output, proof)
    finally:
        if monitor is not None:
            proof["health"] = monitor.finish()
        if capture is not None:
            proof["cleanup"] = capture.close()
        if proof["cleanup"].get("confirmed") is True:
            try:
                shutil.rmtree(work)
                proof["privateStateRetained"] = False
            except Exception:
                proof["cleanup"].update(confirmed=False, privateCleanupFailed=True)
                proof["privateStateRetained"] = True
        else:
            # Keep failed journals on the disposable host. Never upload them.
            proof["privateStateRetained"] = True
    require(proof.get("flowVerified") is True and proof["cleanup"].get("confirmed") is True
            and proof.get("health", {}).get("failure") is None
            and capture is not None and capture.health_failure is None,
            "flow_or_cleanup_unconfirmed")
    proof.update(passed=True, outcome="completed")


def write_report(path, proof):
    encoded = json.dumps(proof, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if len(encoded.encode()) > MAX_REPORT_BYTES:
        # Never lose state outcomes/cleanup because verbose sampled metadata overflows.
        proof["health"] = {"failure": "report_size_limit", "samplesOmitted": True}
        proof.update(passed=False, outcome="report_size_limit")
        encoded = json.dumps(proof, sort_keys=True, indent=2, allow_nan=False) + "\n"
    require(len(encoded.encode()) <= MAX_REPORT_BYTES, "report_size_limit")
    path.write_text(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--owned-simulator-name", required=True)
    parser.add_argument("--helper-products", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output = args.output.absolute()
    args.helper_products = args.helper_products.absolute()
    require(not args.output.exists(), "new_output_required")
    args.output.mkdir(mode=0o700)
    proof = new_report()
    try:
        execute(args, proof)
    except Exception as error:
        proof["outcome"] = "failed"
        # Native exceptions can contain private paths or tool output.
        proof["failureType"] = type(error).__name__
        if isinstance(error, DiagnosticError) and re.fullmatch(r"[a-z0-9_-]{1,100}", str(error)):
            proof["failureCode"] = str(error)
    finally:
        write_report(args.output / "result.json", proof)
    print(json.dumps({"outcome": proof["outcome"], "observerCalls": proof["observerCalls"],
                      "inputsAttempted": proof["inputsAttempted"], "passed": proof["passed"]}))
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
