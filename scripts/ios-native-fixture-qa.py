#!/usr/bin/env python3
"""CI-owned Simulator proof of real configured UIKit gesture/pixel/log contracts.

Never selects a user's device, installs tools, accepts licenses, resets app data,
uses signing identities, or claims generic semantic replay. Requires the workflow
to create, boot, and later remove the exact uniquely named ephemeral Simulator.
"""
from __future__ import annotations

import argparse
import base64
import copy
import functools
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

FIXTURE_REVISION = "inventory-empty-statement-v2"
PIXEL_ORACLE_REVISION = "exact-required-observed-forbidden-v2"
OBSERVER_PROCESS_TIMEOUT_SECONDS = 15
PIXEL_STATE_TIMEOUT_SECONDS = 25

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproof.core import digest
from reproof.ios_instrumentation import prepare_ios_instrumentation
from reproof.ios_observation import build_observation_app, load_observation_profile
from reproof.ios_profile import validate_ios_profile
from reproof.ios_runner import prepare_xctestrun, _targets
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from reproof.live.model import Lab, LiveError
from reproof.live import providers as ios_providers
from reproof.live.providers import IosProvider, ios_device, require_ios_helper_v2
from reproof.live.server import LiveServer


class GateFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise GateFailure(code)


def save(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def owned_temporary_root(temporary):
    # macOS commonly returns /var/folders/... while /var aliases /private/var.
    # Resolve only our newly created directory, before passing new child outputs
    # into the production artifact boundary, which still rejects symlink paths.
    path = Path(temporary.name)
    require(path.is_dir() and not path.is_symlink(), "owned_temporary_root_required")
    return path.resolve(strict=True)


def command(argv, *, timeout=30):
    result = subprocess.run(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if result.returncode:
        # Fixture/tool output is synthetic, but do not echo an arbitrary environment.
        raise GateFailure("command_failed: " + str(argv[0]) + ": " +
                          result.stderr.decode(errors="replace")[-3000:])
    return result.stdout


def verify_ci_simulator(simulator, owned_name):
    require(platform.system() == "Darwin" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "macOS"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("GITHUB_REPOSITORY") == "ictechgy/reproof", "github_macos_ci_required")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[0-9]+", run_id) and re.fullmatch(r"[0-9]+", attempt),
            "ci_ownership_metadata_required")
    require(owned_name == f"reproof-ci-ios-{run_id}-{attempt}", "owned_simulator_name_mismatch")
    require(re.fullmatch(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}", simulator),
            "explicit_simulator_uuid_required")
    document = json.loads(command(["/usr/bin/xcrun", "simctl", "list", "devices", "available", "--json"]))
    matches = [(runtime, device) for runtime, devices in document["devices"].items()
               for device in devices if device.get("udid") == simulator]
    require(len(matches) == 1, "owned_simulator_not_unique")
    runtime, device = matches[0]
    require(device.get("name") == owned_name and device.get("isAvailable") is True
            and device.get("state") == "Booted", "owned_simulator_not_booted_or_wrong_name")
    require(".iOS-" in runtime, "ios_runtime_required")
    return {"name": owned_name, "runtime": runtime, "deviceType": device.get("deviceTypeIdentifier"),
            "runId": run_id, "runAttempt": attempt}


def runtime_profile(build, *, label, logs):
    receipt = build["receipt"]
    document = {
        "schemaVersion": 2, "kind": "reproof-runtime-application", "id": "inventory_" + label,
        "projectId": "inventory_ci", "projectDigest": digest({"fixture": "offline-inventory-v1"}),
        "applicationId": "inventory_ios", "buildId": label, "platform": "ios",
        "bundle": receipt["applicationId"],
        "launchTarget": {"kind": "bundle", "value": receipt["applicationId"]},
        "artifact": {"kind": "ios-app", "sha256": receipt["artifactDigest"],
                     "bytes": receipt["artifactBytes"], "provenanceDigest": receipt["sourceDigest"],
                     "bundleVersion": receipt["bundleVersion"], "bundleBuild": receipt["bundleBuild"]},
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {
            "actions": ["tap", "text", "home", "launch"], "locator": None,
            "observations": ["pixels", "logs"] if logs else ["pixels"],
            "geometry": {"maxWidth": 4096, "maxHeight": 4096, "orientations": ["portrait"]},
            "captureAdapter": {"id": "native-frame", "version": 1},
            "logAdapter": {"id": "repro-app-log", "version": 1} if logs else None,
        },
        "approvedReferences": {"launch": "launch_inventory", "preparations": []},
        "identityRequirement": "install-and-launch",
    }
    return validate_ios_profile(document)


def normalized_words(value):
    require(type(value) is dict and type(value.get("observations")) is list, "pixel_observer_invalid")
    for row in value["observations"]:
        require(type(row) is dict and type(row.get("text")) is str
                and type(row.get("confidence")) in (int, float)
                and math.isfinite(row["confidence"]) and 0 <= row["confidence"] <= 1,
                "pixel_observer_invalid")
    return {" ".join(row["text"].upper().split())
            for row in value["observations"] if row["confidence"] >= 0.9}


def state_matches(value, required, forbidden):
    # Required text must be exact and confident. An exact forbidden reading,
    # even uncertain, cannot become evidence of absence by being filtered out.
    words = normalized_words(value)
    observed = {" ".join(row["text"].upper().split()) for row in value["observations"]}
    return set(required) <= words and not (set(forbidden) & observed)



OBSERVER_STAGES = frozenset({"started", "invalid_arguments", "image_read", "vision_started",
                             "vision_completed", "output_written", "failed"})


def observer_stages(stderr):
    """Retain only bounded, exact stage/timing records, including on timeout."""
    if isinstance(stderr, bytes):
        stderr = stderr[:16384].decode("ascii", errors="replace")
    if not isinstance(stderr, str):
        return []
    result = []
    for line in stderr[:16384].splitlines():
        match = re.fullmatch(r"reproof_observer stage=([a-z_]+) elapsed_ms=([0-9]{1,9})", line)
        if match and match[1] in OBSERVER_STAGES:
            result.append({"stage": match[1], "elapsedMs": int(match[2])})
            if len(result) == 16:
                break
    return result


def observe_frame(observer, path, timeout, attempt):
    # Do not use command(): arbitrary stderr or TimeoutExpired command strings
    # must not escape into the public fixture result. Never retry a timeout.
    process_deadline = time.monotonic() + timeout
    try:
        result = subprocess.run([str(observer), str(path)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        attempt["observerStages"] = observer_stages(error.stderr)
        attempt["processOutcome"] = "timeout"
        raise GateFailure("pixel_observer_timeout") from None
    except OSError:
        attempt["processOutcome"] = "launch_failed"
        raise GateFailure("pixel_observer_launch_failed") from None
    returned = time.monotonic()
    attempt["observerStages"] = observer_stages(result.stderr)
    if returned >= process_deadline:
        attempt["processOutcome"] = "deadline_exceeded"
        raise GateFailure("pixel_observer_deadline_exceeded")
    attempt["processOutcome"] = "returned" if result.returncode == 0 else "failed"
    require(result.returncode == 0, "pixel_observer_failed")
    return result.stdout


def observe_state(frame_source, observer, output, proof, name, required, forbidden=(), *, last_frame=0):
    """One absolute state budget; every accepted OCR result binds its exact JPEG."""
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
            timeout = min(OBSERVER_PROCESS_TIMEOUT_SECONDS, remaining("observer_start"))
            attempt["processTimeoutSeconds"] = timeout
            try:
                raw = observe_frame(observer, path, timeout, attempt)
            except GateFailure:
                remaining("observer_returned")
                raise
            remaining("observer_returned")
            try:
                actual = json.loads(raw)
            except (ValueError, UnicodeError):
                raise GateFailure("pixel_observer_invalid") from None
            remaining("observer_parsed")
            normalized_words(actual)
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
                remaining("before_pass")
                attempt["outcome"] = state["outcome"] = "passed"
                proof["states"].append(accepted)
                return last_frame
            attempt["outcome"] = "mismatch"
            pause()
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


def verify_app_logs(snapshot):
    require(snapshot.get("lostEvents") is False and snapshot.get("truncated") is False,
            "app_log_incomplete")
    events = snapshot["events"]
    observed = {(row["type"], row["name"], row.get("target")) for row in events}
    for target in ("inventory.add", "inventory.save", "inventory.item.0", "inventory.back", "inventory.cancel"):
        require(("click", "began", target) in observed and ("click", "returned", target) in observed,
                "app_log_missing_click_" + target)
    for screen in ("list", "editor", "detail"):
        require(("screen", "appeared", screen) in observed, "app_log_missing_screen_" + screen)
    raw = json.dumps(snapshot)
    require("BLUE MUG" not in raw and "NAME REQUIRED" not in raw,
            "app_log_contains_raw_product_text")
    return {"eventCount": len(events), "runId": snapshot["runId"],
            "profileDigest": snapshot["profileDigest"], "rawTextAbsent": True,
            "declaredClickAndScreenCoverage": True}


def helper_preflight(products, work):
    """Exercise the actual template gates without publishing the private template."""
    config = prepare_xctestrun(products, "ReproLiveTests", work / "helper-preflight.xctestrun")
    with config.open("rb") as stream:
        document = plistlib.load(stream)
    require_ios_helper_v2(document)
    target = next(target for _, target in _targets(document))
    return {"fixedTargetCount": 1, "helperProtocolVersion": 2, "helperVersion": 2,
            "artifactPaths": {key: bool(target.get(key)) and Path(target[key]).exists()
                              for key in ("TestHostPath", "TestBundlePath", "UITargetAppPath")}}


class StartupDiagnostics:
    """Transparent startup-only observation; never saves native output or values.

    Only the exact provider-owned xcodebuild command changes DEVNULL to a pipe.
    A continuously draining thread classifies bounded lines, then discards them.
    After startup it only drains, without inspecting frame/input/cleanup paths.
    """
    ERROR_TYPES = frozenset({"LiveError", "ContractError", "GateFailure", "RuntimeError", "ValueError",
                             "TimeoutError", "TimeoutExpired", "OSError", "FileNotFoundError", "PermissionError"})
    ERROR_CODES = frozenset({"authority_required", "authority_rejected", "authority_expired", "authority_unavailable",
        "startup_rejected", "startup_unknown", "native_protocol_mismatch", "native_profile_mismatch",
        "native_timing_invalid", "app_changed", "app_unavailable", "app_log_unavailable", "cleanup_uncertain"})
    REASONS = {"Provider could not start": "provider_start_failed",
        "Native driver startup timed out": "driver_startup_timeout",
        "Native driver stopped; close and reopen the session": "driver_stopped",
        "Native driver stopped before startup": "driver_stopped_before_startup",
        "iOS helper startup window is closed": "startup_window_closed",
        "iOS helper handshake did not complete": "handshake_incomplete",
        "Operation authority expired": "operation_authority_expired",
        "Dispatch authority was revoked": "dispatch_authority_revoked",
        "Dispatch permit is not current": "dispatch_permit_not_current"}
    APPLE_DOMAINS = frozenset({"IDETestOperationsObserverErrorDomain", "IDEProfileErrorDomain", "IDELaunchErrorDomain",
        "FBSOpenApplicationServiceErrorDomain", "FBSOpenApplicationErrorDomain", "RBSRequestErrorDomain",
        "IXUserPresentableErrorDomain", "MIInstallerErrorDomain", "NSPOSIXErrorDomain", "NSOSStatusErrorDomain"})
    NATIVE_FAILURES = frozenset({"bridge_configuration_invalid", "bridge_request_failed", "authority_expired",
                                 "target_unavailable", "general_profile_invalid"})
    MAX_LINE_BYTES = 4096

    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.originals = []
        self.provider = None
        self.finished = False
        self.drainer = None
        self.data = {"observer": "ios-startup-call-through-v1", "productionClassCodeUnchanged": True,
            "startupObserversRemoved": False, "events": [], "failures": [],
            "xctest": {"stages": [], "nativeFailureCategories": [], "appleErrors": [],
                       "unclassifiedLines": 0, "oversizedLines": 0, "rawOutputPublished": False}}

    @classmethod
    def error(cls, error):
        result = {"type": type(error).__name__ if type(error).__name__ in cls.ERROR_TYPES else "Exception"}
        if isinstance(error, LiveError) and error.code in cls.ERROR_CODES:
            result["code"] = error.code
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in cls.REASONS:
            result["reason"] = cls.REASONS[error.args[0]]
        trace, current = [], error.__traceback__
        while current is not None:
            try:
                relative = Path(current.tb_frame.f_code.co_filename).resolve().relative_to(ROOT).as_posix()
            except (ValueError, OSError):
                relative = ""
            if relative.startswith("reproof/") or relative == "scripts/ios-native-fixture-qa.py":
                trace.append({"file": relative, "line": current.tb_lineno})
            current = current.tb_next
        if trace:
            result["trace"] = trace[-8:]
        return result

    def event(self, phase, outcome, duration=None):
        row = {"phase": phase, "outcome": outcome,
               "sinceStartMs": max(0, round((time.monotonic() - self.started) * 1000))}
        if duration is not None:
            row["durationMs"] = max(0, round(duration * 1000))
        with self.lock:
            if len(self.data["events"]) < 32:
                self.data["events"].append(row)

    def call(self, phase, original, *args, **kwargs):
        self.event(phase, "entered")
        started = time.monotonic()
        try:
            result = original(*args, **kwargs)
        except Exception as error:
            with self.lock:
                if len(self.data["failures"]) < 8:
                    self.data["failures"].append(dict(self.error(error), phase=phase))
            self.event(phase, "raised", time.monotonic() - started)
            raise
        self.event(phase, "returned", time.monotonic() - started)
        return result

    def classify_line(self, raw):
        with self.lock:
            if self.finished:
                return
            output = self.data["xctest"]
            if len(raw) > self.MAX_LINE_BYTES:
                output["oversizedLines"] = min(1_000_000, output["oversizedLines"] + 1)
                return
            line = raw.decode("utf-8", errors="replace").strip()
            recognized = False
            stages = {"Testing started": "testing_started", "** TEST FAILED **": "testing_failed",
                      "** TEST SUCCEEDED **": "testing_succeeded"}
            stage = stages.get(line)
            if re.fullmatch(r"Test Case '-\[ReproLiveTests\.LiveControlTests testControlSession\]' started\.", line):
                stage = "control_test_started"
            if stage:
                if stage not in output["stages"]:
                    output["stages"].append(stage)
                recognized = True
            # Exact known XCTest assertion suffix, never arbitrary sanitized prose.
            marker = re.search(r"error: -\[ReproLiveTests\.LiveControlTests testControlSession\] : (?:failed - |XCTAssertFalse failed - )([a-z_]+)$", line)
            if marker and marker[1] in self.NATIVE_FAILURES:
                if marker[1] not in output["nativeFailureCategories"]:
                    output["nativeFailureCategories"].append(marker[1])
                recognized = True
            for match in re.finditer(r"\bError Domain=([A-Za-z][A-Za-z0-9]+) Code=(-?[0-9]{1,6})(?=\s|$)", line):
                if match[1] in self.APPLE_DOMAINS:
                    row = {"domain": match[1], "code": int(match[2])}
                    if row not in output["appleErrors"] and len(output["appleErrors"]) < 16:
                        output["appleErrors"].append(row)
                    recognized = True
            if not recognized:
                output["unclassifiedLines"] = min(1_000_000, output["unclassifiedLines"] + 1)

    def drain(self, stream):
        pending = b""
        discarding = False
        try:
            while chunk := stream.read(8192):
                if self.finished:
                    pending = b""
                    continue
                for piece in chunk.splitlines(keepends=True):
                    ended = piece.endswith(b"\n") or piece.endswith(b"\r")
                    if not discarding:
                        if len(pending) + len(piece) > self.MAX_LINE_BYTES:
                            pending = b""
                            discarding = True
                            with self.lock:
                                value = self.data["xctest"]["oversizedLines"]
                                self.data["xctest"]["oversizedLines"] = min(1_000_000, value + 1)
                        else:
                            pending += piece
                    if ended:
                        if not discarding:
                            self.classify_line(pending)
                        pending, discarding = b"", False
            if pending and not discarding:
                self.classify_line(pending)
        except Exception:
            with self.lock:
                self.data["xctest"]["drainFailed"] = True
        finally:
            stream.close()

    def popen(self, original, *args, **kwargs):
        provider = self.provider
        if provider is None or provider.temp is None or self.finished:
            return original(*args, **kwargs)
        private = Path(provider.temp.name)
        expected = ["/usr/bin/xcodebuild", "test-without-building", "-xctestrun", str(private / "live.xctestrun"),
            "-destination", "id=" + provider.udid, "-resultBundlePath", str(private / "result.xcresult"),
            "-parallel-testing-enabled", "NO", "-only-testing:ReproLiveTests/LiveControlTests/testControlSession"]
        if len(args) != 1 or args[0] != expected or kwargs != {
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True}:
            return original(*args, **kwargs)
        # Start the drain before dispatch. If observation cannot start, retain
        # the original DEVNULL behavior rather than risking a blocked child.
        read_fd = write_fd = None
        stream = None
        try:
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(read_fd, "rb", buffering=0)
            read_fd = None
            self.drainer = threading.Thread(target=self.drain, args=(stream,), daemon=True)
            self.drainer.start()
        except Exception:
            if stream is not None:
                stream.close()
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            self.data["xctest"]["captureUnavailable"] = True
            return original(*args, **kwargs)
        try:
            # Popen itself and its stdout=None behavior stay unchanged. Only
            # the exact child receives the private write fd, never its argv/env.
            return self.call("xcodebuild_spawn", original, *args,
                             **{**kwargs, "stdout": write_fd, "stderr": write_fd})
        finally:
            os.close(write_fd)

    def observe_provider(self, provider):
        require(type(provider) is IosProvider, "diagnostic_requires_real_ios_provider")
        self.provider = provider
        for name in ("start", "start_authorized", "bridge"):
            original = getattr(provider, name)
            self.originals.append((provider, name, original))
            def wrap(original, name):
                @functools.wraps(original)
                def observed(*args, **kwargs):
                    if name == "bridge":
                        if not args or args[0] not in {"ready", "started"} or self.finished:
                            return original(*args, **kwargs)
                        return self.call("bridge_" + args[0], original, *args, **kwargs)
                    try:
                        return self.call(name, original, *args, **kwargs)
                    finally:
                        if name == "start_authorized":
                            self.finish()
                return observed
            setattr(provider, name, wrap(original, name))
        original_module = ios_providers.subprocess
        self.originals.append((ios_providers, "subprocess", original_module))
        observed_popen = functools.partial(self.popen, original_module.Popen)
        class StartupSubprocess:
            Popen = staticmethod(observed_popen)
            def __getattr__(self, name):
                return getattr(original_module, name)
        # Scope to the provider module: all unrelated subprocess users retain
        # the original Popen class. Restore before Lab can invoke abort/cleanup.
        ios_providers.subprocess = StartupSubprocess()
        return provider

    def finish(self):
        if self.finished:
            return
        state = None
        complete = False
        try:
            provider = self.provider
            if provider is not None:
                process = provider.process
                code = process.poll() if process is not None else None
                # Only an already-exited child can have a short output-drain
                # join. This occurs after startup, never renews its deadline.
                if code is not None and self.drainer is not None:
                    self.drainer.join(timeout=0.1)
                    complete = not self.drainer.is_alive()
                state = {
                    "processCreated": process is not None, "processRunning": process is not None and code is None,
                    "processReturnCode": code if type(code) is int and -255 <= code <= 255 else None,
                    "nativeHandshakeBound": provider.native_handshake is not None,
                    "startedAcknowledged": provider.handshake_ready is not None and provider.handshake_ready.is_set(),
                    "installPending": getattr(provider, "_pending_install_identity", None) is not None,
                    "cancelled": provider.stop.is_set()}
        except Exception:
            # A diagnostic snapshot failure must not mask the original startup
            # exception or prevent Lab's normal quarantining/abort path.
            self.data["snapshotUnavailable"] = True
        with self.lock:
            if self.finished:
                return
            if state is not None:
                self.data["stateAtStartupEnd"] = state
            self.data["xctest"]["completeAtStartupEnd"] = complete
            self.finished = True
            restored = True
            for instance, name, original in reversed(self.originals):
                try:
                    setattr(instance, name, original)
                except Exception:
                    restored = False
            self.originals.clear()
            self.data["startupObserversRemoved"] = restored

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)


def run_flow(args, build, observer, label, work, *, logs):
    # Recheck ownership immediately before installation/startup, not only at entry.
    verify_ci_simulator(args.simulator, args.owned_simulator_name)
    output = args.output / label
    output.mkdir()
    profile = runtime_profile(build, label=label, logs=logs)
    save(output / "runtime-profile.json", profile.data)
    descriptor = ios_device(args.simulator, args.helper_products, profile.bundle,
                            app=build["app"], profile=profile)
    require(descriptor["capabilities"]["locatorKinds"] == [], "semantic_capability_must_be_absent")
    private = work / label
    private.mkdir()
    authority = HostAuthority(private / "authority.sqlite3", lease_directory=private / "leases")
    grant = issue_local_parent_grant(authority, lifetime_ns=600_000_000_000)
    diagnostics = StartupDiagnostics()
    factory = descriptor["factory"]
    descriptor["factory"] = lambda: diagnostics.observe_provider(factory())
    lab = Lab([descriptor], private / "live", authority=authority, parent_grant=grant,
              idle_timeout=120, max_session_seconds=600)
    server = None
    thread = None
    session = None
    proof = {"passed": False, "fixtureRevision": FIXTURE_REVISION,
             "pixelOracleRevision": PIXEL_ORACLE_REVISION, "inputMode": "gesture-batch", "observer": "Apple Vision on real provider JPEG",
             "semanticLocatorSupport": False, "targetBoundTyping": False, "states": [], "receipts": []}
    owner, controller = "ci-owner", "ci-controller"
    try:
        server = LiveServer(lab, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/api/mode")
            response = connection.getresponse()
            diagnostics.data["hostLoopbackReachable"] = response.status == 200 and len(response.read(4097)) <= 4096
        except Exception as error:
            diagnostics.data["hostLoopbackReachable"] = False
            diagnostics.data["hostLoopbackError"] = diagnostics.error(error)
        finally:
            connection.close()
        session = lab.create_session(descriptor["id"], owner, controller)
        diagnostics.data["sessionStateAfterStartup"] = session["state"]
        diagnostics.data["sessionFailureReason"] = diagnostics.REASONS.get(session.get("error"), "none_or_unclassified")
        deadline = time.monotonic() + 95
        while session["state"] == "connecting" and time.monotonic() < deadline:
            time.sleep(0.2)
            session = lab.get_session(session["id"], owner)
        require(session["state"] == "active", "native_provider_not_active: " + str(session.get("error", "")))
        sid = session["id"]
        proof["sessionId"] = sid
        provider = lab.sessions[sid]["provider"]
        require(provider.native_handshake is not None, "native_handshake_missing")
        require(provider.identity["artifactDigest"] == build["receipt"]["artifactDigest"],
                "installed_artifact_mismatch")
        proof["identity"] = {"selectedDigest": build["receipt"]["artifactDigest"],
                             "installedDigest": provider.identity["artifactDigest"],
                             "proof": "readable-simulator-app-tree", "nativeHandshake": True}
        last_frame = 0
        sequence = 0

        def observe(name, required, forbidden=()):
            nonlocal last_frame
            last_frame = observe_state(lambda: lab.frame(sid, owner), observer, output, proof,
                                       name, required, forbidden, last_frame=last_frame)

        def send(action, payload):
            nonlocal sequence, last_frame
            frame = lab.frame(sid, owner)
            sequence += 1
            last_frame = max(last_frame, frame["id"])
            receipt = lab.input(sid, owner, {"controllerId": controller, "epoch": session["epoch"],
                "sequence": sequence, "commandId": uuid.uuid4().hex,
                "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                "action": action, "payload": payload})
            require(receipt["status"] == "injected", "input_not_confirmed")
            proof["receipts"].append(receipt)

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
        if logs:
            snapshot = lab.app_logs(sid, owner)
            save(output / "app-log.json", snapshot)
            proof["appLogs"] = verify_app_logs(snapshot)
        proof["flowVerified"] = True
    finally:
        diagnostics.finish()
        proof["startupDiagnostics"] = diagnostics.snapshot()
        if session is not None:
            try:
                closed = lab.close_session(session["id"], owner, require_finished_inputs=True)
                proof["cleanup"] = {"sessionState": closed["state"],
                    "deviceState": lab.devices[descriptor["id"]]["state"]}
                proof["passed"] = bool(proof.get("flowVerified") and closed["state"] == "closed"
                                       and lab.devices[descriptor["id"]]["state"] == "available")
            except Exception as error:
                proof["cleanup"] = {"confirmed": False, "errorType": type(error).__name__}
        if server is not None:
            # The native bridge stays alive until exact-session cleanup completes.
            for name, operation in (("serverOperations", server.close_operations),
                                    ("lab", lab.close_all), ("server", server.shutdown),
                                    ("serverSocket", server.server_close)):
                try:
                    operation()
                except Exception as error:
                    proof["passed"] = False
                    proof.setdefault("shutdownErrors", []).append({"component": name,
                                                                    "errorType": type(error).__name__})
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    proof["passed"] = False
                    proof.setdefault("shutdownErrors", []).append({"component": "serverThread", "errorType": "still_running"})
        try:
            authority.close()
        except Exception as error:
            proof["passed"] = False
            proof["authorityCleanup"] = {"confirmed": False, "errorType": type(error).__name__}
        finally:
            save(output / "result.json", proof)
    require(proof["passed"], "native_cleanup_unconfirmed")
    return proof


def reject_semantic_claim(args, build):
    profile = copy.deepcopy(runtime_profile(build, label="instrumented", logs=True).data)
    # Runtime locator targets are contract IDs, not UIKit accessibility IDs.
    # Keep this synthetic capability probe valid so it reaches the provider gate.
    profile["capabilities"]["locator"] = {"kind": "xctest-accessibility", "version": 1,
                                            "targets": ["inventory_save"]}
    profile["capabilities"]["observations"].append("accessibility")
    try:
        ios_device(args.simulator, args.helper_products, profile["bundle"],
                   app=build["app"], profile=profile)
    except LiveError as error:
        require(error.code == "unsupported_operation", "wrong_semantic_rejection")
        return {"status": "unsupported", "code": error.code, "beforeProviderStartup": True,
                "coordinateFallback": False}
    raise GateFailure("unsupported_semantic_profile_was_accepted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--owned-simulator-name", required=True)
    parser.add_argument("--helper-products", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output = args.output.absolute()
    args.helper_products = args.helper_products.absolute()
    ownership = verify_ci_simulator(args.simulator, args.owned_simulator_name)
    require(not args.output.exists(), "new_output_required")
    require(args.helper_products.is_dir(), "built_helper_products_required")
    args.output.mkdir(parents=True, mode=0o700)
    result = {"schemaVersion": 1, "kind": "configured-uikit-native-ci-proof", "passed": False, "builds": {},
              "executionEnvironment": "github-owned-ios-simulator", "ownership": ownership,
              "fixtureRevision": FIXTURE_REVISION, "pixelOracleRevision": PIXEL_ORACLE_REVISION,
              "companyAppValidated": False, "physicalDeviceValidated": False,
              "semanticReplayValidated": False, "targetBoundTypingValidated": False,
              "scope": "independent offline UIKit fixture; real Reproof preparation, build, provider, authority, input, pixels and logs"}
    # Build products, authority journals and native/session state stay outside the
    # artifact directory. Only explicit synthetic evidence files are published.
    temporary = tempfile.TemporaryDirectory(prefix="reproof-ios-native-private-")
    try:
        work = owned_temporary_root(temporary)
        result["helperPreflight"] = helper_preflight(args.helper_products, work)
        save(args.output / "result.json", result)
        source = ROOT / "ios-native-fixture"
        profile = load_observation_profile(source / "observation-profile.json")
        preparation = prepare_ios_instrumentation(source, work / "prepared", profile=profile)
        require(preparation["productSourcesUnchanged"] is True, "product_sources_changed")
        original = build_observation_app(source, work / "original-build", profile=profile)
        result["builds"]["original"] = copy.deepcopy(original["receipt"])
        save(args.output / "result.json", result)
        instrumented = build_observation_app(preparation["source"], work / "instrumented-build")
        result["builds"]["instrumented"] = copy.deepcopy(instrumented["receipt"])
        save(args.output / "result.json", result)
        release = build_observation_app(preparation["source"], work / "release-build", configuration="Release")
        result["builds"]["release"] = copy.deepcopy(release["receipt"])
        save(args.output / "result.json", result)
        require(release["receipt"]["automaticObservations"] is False and release["receipt"]["signed"] is False,
                "release_not_uninstrumented_unsigned")
        observer = work / "observe-frame"
        command(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision", "-framework", "Foundation",
                 str(source / "ObserveFrame.swift"), "-o", str(observer)], timeout=90)
        result["unsupportedSemanticProfile"] = reject_semantic_claim(args, instrumented)
        result["original"] = run_flow(args, original, observer, "original", work, logs=False)
        result["instrumented"] = run_flow(args, instrumented, observer, "instrumented", work, logs=True)
        require([row["name"] for row in result["original"]["states"]]
                == [row["name"] for row in result["instrumented"]["states"]], "behavior_comparison_incomplete")
        result["release"] = {"built": True, "automaticObservations": False, "runtimeTested": False,
                             "artifactDigest": release["receipt"]["artifactDigest"]}
        result["passed"] = True
    except Exception as error:
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:4000]}
        raise
    finally:
        try:
            temporary.cleanup()
        except Exception as error:
            result["passed"] = False
            result["privateWorkCleanup"] = {"confirmed": False, "errorType": type(error).__name__}
        save(args.output / "result.json", result)
        print(json.dumps({"passed": result["passed"], "output": str(args.output),
                          "scope": "configured UIKit gestures, pixels and logs; not semantic replay"}), flush=True)
    require(result["passed"], "native_proof_incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
