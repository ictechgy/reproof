#!/usr/bin/env python3
"""Owned Android startup observation only. No UI input or permission changes.

Reconstructed from reviewed conversation history; requires independent review.
Production provider startup deadlines and authenticated authority are unchanged.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

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
TARGETS = ("list_screen",)
OWNER = "ci-startup-owner"
ERROR_CODES = frozenset({
    "android_startup_authority_invalid", "android_startup_cancelled",
    "android_startup_process_exited", "android_startup_transport_unavailable",
    "android_startup_timeout", "android_startup_metadata_rejected",
    "authority_required", "authority_unavailable", "authority_expired",
    "startup_rejected", "startup_unknown", "startup_timeout",
    "native_protocol_mismatch", "native_profile_mismatch", "native_timing_invalid",
    "native_bridge_error", "native_timeout", "helper_config_failed",
    "app_changed", "wrong_app", "app_identity", "install_failed", "forward_failed",
    "invalid_frame", "session_inactive", "cleanup_uncertain", "frame_pending",
})

def sanitized_error(error):
    code = error.code if isinstance(error, LiveError) else None
    return code if code in ERROR_CODES else "unclassified_failure"

def require(condition, message="startup_validation_failed"):
    if not condition:
        raise RuntimeError(message)

def one_shot_guard():
    require(os.environ.get("GITHUB_EVENT_NAME") == "pull_request")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    require(event.get("action") == "opened"
            and pr["head"]["repo"]["full_name"] == "ictechgy/reproof"
            and pr["base"]["repo"]["full_name"] == "ictechgy/reproof"
            and pr["head"]["ref"] == "codex/android-startup-one-shot-validation"
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1")

def environment_guard(serial, owned_name):
    require(sys.platform.startswith("linux"))
    require(os.environ.get("CI") == "true"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("GITHUB_REPOSITORY") == "ictechgy/reproof"
            and os.environ.get("REPROOF_OWNED_ANDROID_EMULATOR") == "1")
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[0-9]+", run_id) and attempt == "1"
            and owned_name == f"reproof-ci-{run_id}-{attempt}")
    require(serial == "emulator-5554")
    require(os.environ.get("ANDROID_SERIAL", serial) == serial)
    require(not any(os.environ.get(key) for key in (
        "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT")))

def owned_output_path(raw):
    temp_raw = os.environ["RUNNER_TEMP"]
    temp = Path(temp_raw)
    require(temp.is_absolute() and temp.is_dir() and str(temp) == temp_raw
            and temp.resolve() == temp)
    expected = temp / "reproof-startup-evidence" / "android"
    require(type(raw) is str and raw == str(expected))
    require(not expected.exists() and not expected.is_symlink())
    require(all(not path.is_symlink() for path in (expected, *expected.parents)))
    require(expected.resolve() == expected)
    return expected

def owned_apk_paths(app_argument, helper_argument):
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    require(re.fullmatch(r"[0-9]+", run_id) and os.environ.get("GITHUB_RUN_ATTEMPT") == "1")
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    require(runner_temp.is_absolute() and runner_temp == runner_temp.resolve())
    expected_tmp = runner_temp / f"reproof-startup-private-reproof-ci-{run_id}-1" / "tmp"
    actual_tmp = Path(os.environ["TMPDIR"])
    require(actual_tmp == expected_tmp and actual_tmp.resolve() == expected_tmp)
    build = actual_tmp.parent / "android-build"
    app = build / "runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk"
    helper = build / "live/build/outputs/apk/debug/live-debug.apk"
    require(app_argument == app and app_argument.resolve() == app
            and helper_argument == helper and helper_argument.resolve() == helper)
    return app, helper

def preflight(args):
    environment_guard(args.serial, args.owned_emulator_name)
    one_shot_guard()
    app, helper = owned_apk_paths(args.app_apk, args.helper_apk)
    require(app.is_file() and helper.is_file())
    device = AdbDevice(args.serial)
    rows = [line.split() for line in device._command([device.adb, "devices"]).decode().splitlines()[1:] if line.strip()]
    require(rows == [[args.serial, "device"]])
    require(device.shell("getprop", "ro.kernel.qemu").strip() == "1")
    actual_name = device.adb_call("emu", "avd", "name").decode().replace("\r", "").splitlines()
    require(actual_name == [args.owned_emulator_name, "OK"])
    require(device.shell("getprop", "sys.boot_completed").strip() == "1")
    require(device.apk_identity(app) == {"package": PACKAGE, "versionCode": 1})
    require(device.apk_identity(helper) == {"package": "io.reproof.live", "versionCode": 2})
    sdk = Path(device.adb).resolve().parent.parent
    aapt = sorted((sdk / "build-tools").glob("*/aapt"), reverse=True)[0]
    permissions = device._command([str(aapt), "dump", "permissions", str(app)]).decode()
    require("uses-permission" not in permissions)
    for package in (PACKAGE, "io.reproof.live"):
        require(f"package:{package}" not in device.shell("pm", "list", "packages", package).splitlines())
    require(not device.adb_call("forward", "--list").decode().strip())
    return device, app, helper

def runtime_profile(app, helper):
    source_files = [ROOT / "android/runtime-fixture/build.gradle.kts"]
    source_files += sorted((ROOT / "android/runtime-fixture/src").rglob("*"))
    sources = {path.relative_to(ROOT).as_posix(): sha_file(path) for path in source_files if path.is_file()}
    provenance = {"fixtureSources": sources, "appSha256": sha_file(app), "helperSha256": sha_file(helper)}
    profile = validate_android_runtime_profile({
        "schemaVersion": 2, "kind": "reproof-runtime-application",
        "id": "inventory_android", "projectId": "inventory_fixture",
        "projectDigest": digest(sources), "applicationId": "inventory_app",
        "buildId": "ci_debug", "platform": "android", "package": PACKAGE,
        "launchTarget": {"kind": "activity", "value": ".MainActivity"},
        "artifact": {"kind": "android-apk", "sha256": sha_file(app), "bytes": app.stat().st_size,
                     "provenanceDigest": digest(provenance), "versionCode": 1},
        "helper": {"protocolVersion": 2, "version": 2},
        "capabilities": {"actions": ["launch"],
            "locator": {"kind": "android-resource-id", "version": 1, "targets": list(TARGETS)},
            "observations": ["pixels", "accessibility"],
            "geometry": {"maxWidth": 960, "maxHeight": 4096, "orientations": ["portrait"]},
            "captureAdapter": {"id": "native-frame", "version": 1}, "logAdapter": None},
        "approvedReferences": {"launch": "launch_inventory", "preparations": []},
        "identityRequirement": "installed-sha256"})
    return profile, provenance

def cleanup(lab, authority, device, provider):
    """Independent attempts; quarantine or a failed stage never means success."""
    states = {"sessionHelper": lab is None, "forward": device is None,
              "authority": authority is None, "maintenance": lab is None}
    errors = []
    if lab is not None:
        closed = []
        for sid in list(lab.sessions):
            try:
                closed.append(lab.close_session(sid, OWNER)["state"] == "closed")
            except Exception as error:
                closed.append(False)
                errors.append(sanitized_error(error))
        states["sessionHelper"] = all(closed) and all(item["state"] == "available" for item in lab.devices.values())
        try:
            lab.stop_maintenance()
            states["maintenance"] = True
        except Exception as error:
            errors.append(sanitized_error(error))
    if device is not None:
        try:
            port = getattr(provider, "port", None)
            rows = device.adb_call("forward", "--list", timeout=5).decode().splitlines()
            if type(port) is int and 1024 <= port <= 65535:
                expected = [device.serial, "tcp:" + str(port), "tcp:8766"]
                if any(row.split() == expected for row in rows):
                    device.adb_call("forward", "--remove", expected[1], timeout=5)
            states["forward"] = not device.adb_call("forward", "--list", timeout=5).strip()
        except Exception as error:
            errors.append(sanitized_error(error))
    if authority is not None:
        try:
            authority.close()
            states["authority"] = True
        except Exception as error:
            errors.append(sanitized_error(error))
    return {"confirmed": all(states.values()) and not errors, "stages": states, "errors": errors[:8]}

def validate_startup(lab, session, provider, app, output):
    require(type(provider) is AndroidLiveProvider and provider.general_profile)
    handshake = provider.native_handshake
    require(provider.device_authority is not None and handshake is not None)
    require(handshake.protocol_version == 2 and handshake.helper_version == 2)
    require(provider.handshake_ready.is_set() and provider.helper_ready.is_set() and provider.target_launched)
    require(provider.installed_identity_evidence["installedArtifactDigest"] == sha_file(app))
    sid = session["id"]
    first = lab.frame(sid, OWNER)["id"]
    deadline = time.monotonic() + 5
    while True:
        current = lab.get_session(sid, OWNER)
        require(current["state"] == "active" and current["lastSequence"] == 0)
        frame = lab.frame(sid, OWNER)
        if frame["id"] > first:
            break
        require(time.monotonic() < deadline)
        time.sleep(.05)
    require(type(frame["imageBase64"]) is str and len(frame["imageBase64"]) <= 8_000_000)
    image = base64.b64decode(frame["imageBase64"], validate=True)
    require(frame["mime"] == "image/jpeg" and image[:2] == b"\xff\xd8" and image[-2:] == b"\xff\xd9"
            and 0 < frame["width"] <= 960 and 0 < frame["height"] <= 4096)
    path = output / "first-frame.jpg"
    path.write_bytes(image)
    return {"authenticatedHandshake": True, "installedIdentityVerified": True,
            "ownedTargetLaunched": True, "freshFrame": True, "nativeInputs": 0, "imageSha256": sha_file(path)}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--owned-emulator-name", required=True)
    parser.add_argument("--helper-apk", type=Path, required=True)
    parser.add_argument("--app-apk", type=Path, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = {"schemaVersion": 1, "passed": False, "nativeInputs": 0}
    lab = authority = device = provider = None
    try:
        environment_guard(args.serial, args.owned_emulator_name)
        one_shot_guard()
        output = owned_output_path(args.output)
        owned_apk_paths(args.app_apk, args.helper_apk)
        private_tmp = Path(os.environ["TMPDIR"])
        output.mkdir(parents=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="reproof-startup-authority-", dir=private_tmp) as directory:
            try:
                device, app, helper = preflight(args)
                profile, provenance = runtime_profile(app, helper)
                write_json(output / "build-provenance.json", provenance)
                private = Path(directory)
                authority = HostAuthority(private / "authority.sqlite3", lease_directory=private / "leases")
                grant = issue_local_parent_grant(authority, lifetime_ns=600_000_000_000)
                descriptor = android_live_device(args.serial, helper, app, runtime_profile=profile)
                factory = descriptor["factory"]
                def capture_provider():
                    nonlocal provider
                    provider = factory()
                    return provider
                descriptor["factory"] = capture_provider
                lab = Lab([descriptor], private / "live", authority=authority, parent_grant=grant,
                          idle_timeout=120, max_session_seconds=600)
                session = lab.create_session(descriptor["id"], OWNER, "ci-startup-controller")
                # Observation only: no extension or retry of production startup.
                deadline = time.monotonic() + 30
                while session["state"] == "connecting" and time.monotonic() < deadline:
                    time.sleep(.1)
                    session = lab.get_session(session["id"])
                code = session.get("errorCode")
                if session["state"] != "active":
                    if code in ERROR_CODES:
                        raise LiveError(code, "Startup failed")
                    raise RuntimeError("startup_validation_failed")
                result.update(validate_startup(lab, session, provider, app, output))
            except Exception as error:
                result["failure"] = sanitized_error(error)
            finally:
                result["cleanup"] = cleanup(lab, authority, device, provider)
            result["passed"] = result.get("freshFrame") is True and result["cleanup"]["confirmed"] and "failure" not in result
            write_json(output / "result.json", result)
    except Exception as error:
        result["failure"] = sanitized_error(error)
        result["passed"] = False
    print(json.dumps({key: result[key] for key in ("passed", "nativeInputs", "failure") if key in result}, sort_keys=True))
    return 0 if result["passed"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
