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
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproof.core import digest
from reproof.ios_instrumentation import prepare_ios_instrumentation
from reproof.ios_observation import build_observation_app, load_observation_profile
from reproof.ios_profile import validate_ios_profile
from reproof.live.authority import HostAuthority, issue_local_parent_grant
from reproof.live.model import Lab, LiveError
from reproof.live.providers import ios_device
from reproof.live.server import LiveServer


class GateFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise GateFailure(code)


def save(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


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
    return {" ".join(row["text"].upper().split())
            for row in value["observations"]
            if type(row) is dict and type(row.get("text")) is str
            and type(row.get("confidence")) in (int, float) and row["confidence"] >= 0.9}


def state_matches(words, required, forbidden):
    return set(required) <= words and not (set(forbidden) & words)


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
    lab = Lab([descriptor], private / "live", authority=authority, parent_grant=grant,
              idle_timeout=120, max_session_seconds=600)
    server = None
    thread = None
    session = None
    proof = {"passed": False, "inputMode": "gesture-batch", "observer": "Apple Vision on real provider JPEG",
             "semanticLocatorSupport": False, "targetBoundTyping": False, "states": [], "receipts": []}
    owner, controller = "ci-owner", "ci-controller"
    try:
        server = LiveServer(lab, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        session = lab.create_session(descriptor["id"], owner, controller)
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
            deadline = time.monotonic() + 25
            last_words = set()
            while time.monotonic() < deadline:
                try:
                    frame = lab.frame(sid, owner)
                except LiveError as error:
                    if error.code != "frame_pending":
                        raise
                    time.sleep(0.2)
                    continue
                if frame["id"] <= last_frame:
                    time.sleep(0.2)
                    continue
                last_frame = frame["id"]
                image = base64.b64decode(frame["imageBase64"], validate=True)
                path = output / (name + ".jpg")
                path.write_bytes(image)
                actual = json.loads(command([str(observer), str(path)], timeout=15))
                last_words = normalized_words(actual)
                save(output / (name + "-ocr.json"), actual)
                if state_matches(last_words, required, forbidden):
                    # Prove the very same oracle cannot silently accept a wrong state.
                    require(not state_matches(last_words, ["IMPOSSIBLE FIXTURE STATE"], []),
                            "pixel_oracle_false_positive")
                    proof["states"].append({"name": name, "frameId": frame["id"],
                        "geometryVersion": frame["geometryVersion"], "imageSha256": hashlib.sha256(image).hexdigest(),
                        "required": list(required), "forbidden": list(forbidden), "passed": True})
                    return
                time.sleep(0.2)
            raise GateFailure("pixel_state_unconfirmed_" + name + ": " + repr(sorted(last_words)))

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

        observe("01-empty", ["INVENTORY LIST", "ITEM TOTAL ZERO", "NO ITEMS"], ["BLUE MUG"])
        tap(0.34)
        observe("02-editor", ["CREATE ITEM", "ITEM TOTAL ZERO", "SAVE ITEM"])
        tap(0.44)
        observe("03-invalid", ["CREATE ITEM", "ITEM TOTAL ZERO", "NAME REQUIRED"], ["ITEM TOTAL ONE"])
        tap(0.32)
        send("text", {"value": "BLUE MUG"})
        tap(0.44)
        observe("04-created", ["INVENTORY LIST", "ITEM TOTAL ONE", "BLUE MUG"], ["NO ITEMS", "NAME REQUIRED"])
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
    profile["capabilities"]["locator"] = {"kind": "xctest-accessibility", "version": 1,
                                            "targets": ["inventory.save"]}
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
    result = {"schemaVersion": 1, "kind": "configured-uikit-native-ci-proof", "passed": False,
              "executionEnvironment": "github-owned-ios-simulator", "ownership": ownership,
              "companyAppValidated": False, "physicalDeviceValidated": False,
              "semanticReplayValidated": False, "targetBoundTypingValidated": False,
              "scope": "independent offline UIKit fixture; real Reproof preparation, build, provider, authority, input, pixels and logs"}
    # Build products, authority journals and native/session state stay outside the
    # artifact directory. Only explicit synthetic evidence files are published.
    temporary = tempfile.TemporaryDirectory(prefix="reproof-ios-native-private-")
    work = Path(temporary.name)
    try:
        source = ROOT / "ios-native-fixture"
        profile = load_observation_profile(source / "observation-profile.json")
        preparation = prepare_ios_instrumentation(source, work / "prepared", profile=profile)
        require(preparation["productSourcesUnchanged"] is True, "product_sources_changed")
        original = build_observation_app(source, work / "original-build", profile=profile)
        instrumented = build_observation_app(preparation["source"], work / "instrumented-build")
        release = build_observation_app(preparation["source"], work / "release-build", configuration="Release")
        require(release["receipt"]["automaticObservations"] is False and release["receipt"]["signed"] is False,
                "release_not_uninstrumented_unsigned")
        observer = work / "observe-frame"
        command(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision", "-framework", "Foundation",
                 str(source / "ObserveFrame.swift"), "-o", str(observer)], timeout=90)
        result["unsupportedSemanticProfile"] = reject_semantic_claim(args, instrumented)
        result["builds"] = {label: build["receipt"] for label, build in (
            ("original", original), ("instrumented", instrumented), ("release", release))}
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
