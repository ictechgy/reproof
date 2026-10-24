#!/usr/bin/env python3
"""Four-call diagnostic: identical pinned JPEGs during live capture and after cleanup.

No native acceptance claim. No UI business flow, live-frame OCR, observer retry,
image transformation, external service, or production source modification.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OFFLINE = ROOT / "diagnostics/offline-vision"
REPLAY_SHA256 = "e26ed40b5a8a89851d929e184b243dfa27ceec8a0fc0c8fde5a96d33211faf20"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-18-5"
DEVICE = "com.apple.CoreSimulator.SimDeviceType.iPhone-16"
POLL_SECONDS = 0.25
MAX_POLL_GAP = 1.0
MAX_FRAME_GAP = 2.0
MAX_MONITOR_SECONDS = 45
MAX_SAMPLES = 200
MAX_REPORT_BYTES = 262144
OWNER = "ci-capture-owner"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Refuse a changed observer/oracle implementation before importing it.
if hashlib.sha256((OFFLINE / "replay.py").read_bytes()).hexdigest() != REPLAY_SHA256:
    raise RuntimeError("pinned_replay_source_mismatch")
replay = load_module("capture_comparison_original_replay", OFFLINE / "replay.py")
native = load_module("capture_comparison_native_fixture", ROOT / "scripts/ios-native-fixture-qa.py")
require = replay.require


def command(argv, *, timeout=30):
    """Keep all native command output private; publish only static codes."""
    try:
        return native.command(argv, timeout=timeout)
    except Exception:
        raise replay.DiagnosticError("native_command_failed") from None


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


class CaptureMonitor:
    """Bounded metadata-only samples; cached frames alone are never active proof."""
    def __init__(self, sample):
        self.sample_source = sample
        self.started = time.monotonic()
        self.samples = []
        self.publications = {}
        self.failure = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.thread_started = False

    def poll(self):
        with self.lock:
            now = time.monotonic()
            try:
                require(self.failure is None, "capture_monitor_already_failed")
                require(now - self.started <= MAX_MONITOR_SECONDS and len(self.samples) < MAX_SAMPLES,
                        "capture_monitor_bound_exceeded")
                require(not self.samples or now - self.samples[-1]["at"] <= MAX_POLL_GAP,
                        "capture_monitor_gap")
                data = self.sample_source()
                now = time.monotonic()
                require(type(data) is dict and data.get("active") is True, "capture_inactive")
                require(type(data.get("frameId")) is int and data["frameId"] > 0
                        and type(data.get("frameBytes")) is int and 0 < data["frameBytes"] <= 3 * 1024 * 1024
                        and type(data.get("width")) is int and 0 < data["width"] <= 4096
                        and type(data.get("height")) is int and 0 < data["height"] <= 4096
                        and data.get("mime") == "image/jpeg", "capture_frame_invalid")
                received = data.get("received")
                require(type(received) in (int, float) and 0 <= now - received <= MAX_FRAME_GAP,
                        "capture_frame_stale")
                require(not self.samples or data["frameId"] >= self.samples[-1]["frameId"],
                        "capture_frame_regressed")
                history = data["history"]
                require(type(history) is list and 0 < len(history) <= 30, "capture_history_unknown")
                for frame_id, received_at, geometry in history:
                    require(type(frame_id) is int and 0 < frame_id <= data["frameId"]
                            and type(received_at) in (int, float) and received_at <= now
                            and type(geometry) is int and geometry > 0, "capture_history_invalid")
                    old = self.publications.get(frame_id)
                    require(old is None or old == received_at, "capture_publication_changed")
                    self.publications[frame_id] = received_at
                require(self.publications.get(data["frameId"]) == received, "capture_history_unbound")
                # Max 30 histories per bounded poll, even with malicious fixtures.
                require(len(self.publications) <= MAX_SAMPLES * 30, "capture_history_bound_exceeded")
                row = {key: data[key] for key in ("frameId", "frameBytes", "width", "height")}
                row.update(at=now, frameAgeMs=replay.elapsed_ms(received, now))
                self.samples.append(row)
                return now
            except Exception as error:
                if self.failure is None:
                    self.failure = str(error) if isinstance(error, replay.DiagnosticError) else "capture_state_unknown"
                raise replay.DiagnosticError(self.failure) from None

    def start(self):
        self.poll()
        def monitor():
            while not self.stop.wait(POLL_SECONDS):
                try:
                    self.poll()
                except Exception:
                    return
        self.thread = threading.Thread(target=monitor, name="capture-metadata-monitor", daemon=True)
        self.thread.start()
        self.thread_started = True
        # Capture readiness only, not an OCR warmup. Require advancing publication.
        deadline = time.monotonic() + 3
        initial = self.samples[0]["frameId"]
        while time.monotonic() < deadline:
            self.poll()
            if self.samples[-1]["frameId"] > initial:
                return
            self.stop.wait(POLL_SECONDS)
        raise replay.DiagnosticError("capture_progress_not_established")

    def interval(self, start, end):
        with self.lock:
            require(self.failure is None, self.failure or "capture_state_unknown")
            inside = sorted((stamp, fid) for fid, stamp in self.publications.items() if start < stamp < end)
            require(len(inside) >= 2, "capture_progress_inside_observer_unproven")
            stamps = [start] + [stamp for stamp, _ in inside] + [end]
            gaps = [b - a for a, b in zip(stamps, stamps[1:])]
            require(max(gaps) <= MAX_FRAME_GAP, "capture_progress_gap_inside_observer")
            ids = [fid for _, fid in inside]
            require(all(b > a for a, b in zip(ids, ids[1:])), "capture_progress_regressed")
            polls = [row for row in self.samples if start <= row["at"] <= end]
            poll_stamps = [start] + [row["at"] for row in polls] + [end]
            require(max(b-a for a, b in zip(poll_stamps, poll_stamps[1:])) <= MAX_POLL_GAP,
                    "capture_poll_gap_inside_observer")
            return {"condition": "verified_active", "publicationCountInsideProcess": len(inside),
                    "firstFrameId": ids[0], "lastFrameId": ids[-1],
                    "maxPublicationGapMs": round(max(gaps) * 1000), "pollsInsideProcess": len(polls),
                    "processIntervalMs": replay.elapsed_ms(start, end),
                    "processStartSinceMonitorMs": replay.elapsed_ms(self.started, start),
                    "processEndSinceMonitorMs": replay.elapsed_ms(self.started, end)}

    def finish(self):
        self.stop.set()
        if self.thread_started:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.failure = self.failure or "capture_monitor_stop_unconfirmed"

    def report(self):
        with self.lock:
            return {"pollSeconds": POLL_SECONDS, "maxAllowedFrameGapSeconds": MAX_FRAME_GAP,
                    "maxAllowedPollGapSeconds": MAX_POLL_GAP, "pollCount": len(self.samples),
                    "failure": self.failure, "samples": [dict({k: v for k, v in row.items() if k != "at"},
                        atMs=replay.elapsed_ms(self.started, row["at"])) for row in self.samples]}


class OwnedCapture:
    def __init__(self, args, build, work):
        self.args, self.build, self.work = args, build, work
        self.lab = self.server = self.thread = self.authority = self.session = self.provider = None
        self.handshake = None
        self.thread_started = False
        self.cleanup_attempted = False
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
        self.session = self.lab.create_session(descriptor["id"], OWNER, "ci-capture-controller")
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
                "screenScope": "original offline UIKit fixture initial screen; no business-flow input",
                "createdStateReplicated": False, "liveFramesObservedByVision": 0}

    def sample(self):
        session = self.lab.sessions[self.session["id"]]
        provider = self.provider
        active = (session["state"] == "active" and not session.get("stopAdmission", False)
                  and session["provider"] is provider and provider.native_handshake is self.handshake
                  and provider.handshake_ready is not None and provider.handshake_ready.is_set()
                  and not provider.stop.is_set() and provider.process is not None
                  and provider.process.poll() is None and provider.udid == self.args.simulator
                  and provider.identity["artifactDigest"] == self.build["receipt"]["artifactDigest"])
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


def observe_once(report, executable, image, fixture, work, environment, *, condition, monitor=None):
    """Call the unchanged original observe once; only bracket its process boundary."""
    require(report["observerCalls"] < 4, "observer_call_limit")
    original = replay.run_process
    bracket = {}
    calls = 0
    def process(argv, **kwargs):
        nonlocal calls
        require(calls == 0 and argv == [str(executable), str(image)]
                and kwargs.get("timeout") == 15, "unexpected_observer_process")
        calls += 1
        report["observerCalls"] += 1
        report["callCounts"][condition] += 1
        bracket["start"] = time.monotonic()
        try:
            return original(argv, **kwargs)
        finally:
            bracket["end"] = time.monotonic()
    # Metadata polls are outside replay.observe's original absolute 15s budget.
    if monitor is not None:
        monitor.poll()
    replay.run_process = process
    try:
        attempt = replay.observe(executable, image, fixture, work, environment)
    finally:
        replay.run_process = original
    require(calls == 1, "observer_process_missing")
    if monitor is not None:
        try:
            monitor.poll()
        except replay.DiagnosticError:
            pass
    # Original parsing/oracle is unchanged. Do not publish arbitrary OCR text.
    attempt["observationCount"] = len(attempt.pop("observations", []))
    attempt.update(conditionRequested=condition, actualCondition="unknown",
                   processWallElapsedMs=replay.elapsed_ms(bracket["start"], bracket["end"]))
    report["attempts"].append(attempt)
    if monitor is not None:
        try:
            evidence = monitor.interval(bracket["start"], bracket["end"])
        except replay.DiagnosticError as error:
            attempt["captureConditionFailure"] = str(error)
            raise
        attempt.update(actualCondition="active", captureEvidence=evidence)
    else:
        attempt["actualCondition"] = "session_closed_simulator_shutdown"
    return attempt


def compare_conditions(report, capture, shutdown, executable, images, work, environment):
    monitor = None
    active_valid = False
    try:
        report["capture"] = capture.start()
        monitor = CaptureMonitor(capture.sample)
        monitor.start()
        for image, fixture in zip(images, replay.FIXTURES):
            observe_once(report, executable, image, fixture, work, environment,
                         condition="active", monitor=monitor)
        monitor.poll()
        active_valid = True
    finally:
        if monitor is not None:
            monitor.finish()
            report["captureMonitor"] = monitor.report()
        report["cleanup"] = capture.close()
        # Separate best-effort simulator teardown does not change cleanup verdict.
        try:
            report["simulatorShutdown"] = shutdown()
        except Exception:
            report["simulatorShutdown"] = {"state": "unconfirmed"}
    require(active_valid and monitor.failure is None, "active_condition_unconfirmed")
    require(report["cleanup"].get("confirmed") is True, "exact_session_cleanup_unconfirmed")
    require(report["simulatorShutdown"].get("state") == "Shutdown", "owned_simulator_shutdown_unconfirmed")
    for image, fixture in zip(images, replay.FIXTURES):
        observe_once(report, executable, image, fixture, work, environment, condition="after_cleanup")
    require(report["observerCalls"] == 4 and len(report["attempts"]) == 4, "four_observations_incomplete")
    report["comparisonValid"] = True
    report["diagnosticPassed"] = all(attempt["passed"] for attempt in report["attempts"])
    report["outcome"] = "completed"


def new_report():
    return {"schemaVersion": 1, "kind": "capture-vision-four-observation-comparison", "nativeAcceptance": False,
            "comparisonValid": False, "diagnosticPassed": False, "outcome": "not_started",
            "sourceCommit": "cf1a43d3c15e0152f405f226ec0efc9bd9c264e6", "sourceRunId": "40068410548",
            "nativeBaselineCommit": "6eb207897f5819bf74b949c062a88e67df810abd",
            "observerSha256": replay.OBSERVER_SHA256, "oracleSourceSha256": REPLAY_SHA256,
            "pixelOracleRevision": replay.PIXEL_ORACLE_REVISION, "observerTimeoutSeconds": 15,
            "observerCalls": 0, "callCounts": {"active": 0, "after_cleanup": 0},
            "attempts": [], "expectedObserverCalls": 4,
            "limits": ["Initial-screen capture only, not original created-state or input workload",
                       "Active condition runs first; cache and order effects are not controlled",
                       "One run cannot establish causation or repeatable reliability",
                       "Bounded sampling shows observed activity, not continuous mathematical proof"],
            "inputValidation": "not_started", "host": {"status": "not_checked"},
            "cleanup": {"confirmed": False, "state": "not_started"}}


def execute(report, args):
    source, image_bytes = replay.validate_inputs(OFFLINE)
    report["inputValidation"] = "verified"
    report["implementationHashes"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("diagnostics/capture-vision/compare.py", "scripts/ios-native-fixture-qa.py",
                     "reproof/live/providers.py", "live-ios/Tests/LiveControlTests.swift")}
    work = Path(tempfile.mkdtemp(prefix="capture-vision-private-")).resolve(strict=True)
    try:
        environment = os.environ.copy()
        environment["DEVELOPER_DIR"] = replay.DEVELOPER_DIR
        report["host"] = replay.verify_host(work, environment)
        report["ownership"] = native.verify_ci_simulator(args.simulator, args.owned_simulator_name)
        report["simulator"] = simulator_state(args.simulator, args.owned_simulator_name, "Booted")
        require(args.helper_products.is_dir(), "built_helper_products_required")
        report["helperPreflight"] = native.helper_preflight(args.helper_products, work)
        observer_source, executable = work / "ObserveFrame.swift", work / "observe-frame"
        replay.immutable_copy(observer_source, source)
        images = [work / fixture["file"] for fixture in replay.FIXTURES]
        for path, data in zip(images, image_bytes):
            replay.immutable_copy(path, data)
        report["compiler"] = replay.compile_observer(observer_source, executable, work, environment)
        require(report["compiler"]["outcome"] == "compiled", "observer_compile_failed")
        fixture_source = ROOT / "ios-native-fixture"
        profile = native.load_observation_profile(fixture_source / "observation-profile.json")
        build = native.build_observation_app(fixture_source, work / "original-build", profile=profile)
        receipt = build["receipt"]
        require(receipt["signed"] is False and receipt["automaticObservations"] is False,
                "original_unsigned_fixture_required")
        report["build"] = {key: receipt[key] for key in ("sourceDigest", "artifactDigest", "artifactBytes",
                           "applicationId", "signed", "automaticObservations")}
        capture = OwnedCapture(args, build, work)
        compare_conditions(report, capture, lambda: shutdown_simulator(args.simulator, args.owned_simulator_name),
                           executable, images, work, environment)
    finally:
        if report["cleanup"].get("confirmed") is True:
            shutil.rmtree(work)
        else:
            report["privateStateRetained"] = True


def run(args, stdout=None):
    report = new_report()
    stdout = sys.stdout if stdout is None else stdout
    try:
        destination = args.output.open("x", encoding="utf-8")
    except OSError:
        report["outcome"] = "output_reservation_failed"
        stdout.write(replay.encode_report(report))
        return report
    try:
        execute(report, args)
    except replay.DiagnosticError as error:
        report["outcome"] = str(error)
    except Exception:
        report["outcome"] = "unexpected_local_failure"
    encoded = replay.encode_report(report)
    if len(encoded.encode()) > MAX_REPORT_BYTES:
        report["captureMonitor"] = {"failure": "report_size_limit"}
        report.update(comparisonValid=False, diagnosticPassed=False, outcome="report_size_limit")
    try:
        destination.write(replay.encode_report(report))
        destination.flush()
    except (OSError, ValueError):
        report.update(comparisonValid=False, diagnosticPassed=False, outputFailure="write_failed")
    try:
        destination.close()
    except (OSError, ValueError):
        report.update(comparisonValid=False, diagnosticPassed=False, outputFailure="close_failed")
    stdout.write(replay.encode_report(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--owned-simulator-name", required=True)
    parser.add_argument("--helper-products", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.helper_products = args.helper_products.absolute()
    report = run(args)
    return 0 if report["comparisonValid"] and report["diagnosticPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
