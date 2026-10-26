#!/usr/bin/env python3
"""Reconstructed local proposal: one authorized CI attempt; no ACL remediation.

Subprocess output is discarded except bounded machine checks. Cleanup is not a
second runtime attempt. This reconstruction requires fresh independent review.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "codex/android-startup-one-shot-validation"
REPO = "ictechgy/reproof"
SERIAL = "emulator-5554"
IMAGE = "system-images;android-35;default;x86_64"
WRAPPER = "7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172"
SETTINGS_OVERLAY = '\ninclude(":runtime-fixture")\n'

class Stop(Exception):
    pass

def require(value, code):
    if not value:
        raise Stop(code)

def guard(env):
    require(sys.platform == "linux" and env.get("CI") == "true"
            and env.get("GITHUB_ACTIONS") == "true"
            and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and env.get("GITHUB_REPOSITORY") == REPO
            and env.get("GITHUB_EVENT_NAME") == "pull_request"
            and env.get("GITHUB_RUN_ATTEMPT") == "1"
            and re.fullmatch(r"[0-9]+", env.get("GITHUB_RUN_ID", "")), "one_shot_guard_rejected")
    require(not any(env.get(key) for key in ("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS",
            "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT")), "remote_adb_rejected")
    require(env.get("ANDROID_SERIAL", SERIAL) == SERIAL, "serial_rejected")
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    head = event.get("pull_request", {}).get("head", {})
    require(event.get("action") == "opened" and head.get("ref") == BRANCH
            and head.get("repo", {}).get("full_name") == REPO
            and event.get("repository", {}).get("full_name") == REPO, "one_shot_guard_rejected")
    temp = Path(env["RUNNER_TEMP"])
    require(temp.is_absolute() and temp.is_dir() and not temp.is_symlink(), "temp_rejected")
    return temp.resolve(), f"reproof-ci-{env['GITHUB_RUN_ID']}-1"

def verify_source():
    manifest = json.loads((ROOT / "scripts/ci/android-startup-source.json").read_text())
    require(bool(manifest["files"]), "source_pin_rejected")
    for relative, expected in manifest["files"].items():
        path = ROOT / relative
        require(not Path(relative).is_absolute() and ".." not in Path(relative).parts
                and path.is_file() and not path.is_symlink()
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected, "source_pin_rejected")

def prepare_build(private):
    """Copy only pinned Android inputs; never edit production build settings."""
    manifest = json.loads((ROOT / "scripts/ci/android-startup-source.json").read_text())
    build = private / "android-build"
    build.mkdir(mode=0o700)
    for relative, expected in manifest["files"].items():
        if not relative.startswith("android/"):
            continue
        source = ROOT / relative
        require(not Path(relative).is_absolute() and ".." not in Path(relative).parts
                and source.is_file() and not source.is_symlink()
                and hashlib.sha256(source.read_bytes()).hexdigest() == expected, "source_pin_rejected")
        destination = build / Path(relative).relative_to("android")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, destination)
    settings = build / "settings.gradle.kts"
    original = settings.read_bytes()
    require(b'include(":runtime-fixture")' not in original, "settings_overlay_rejected")
    settings.write_bytes(original + SETTINGS_OVERLAY.encode("utf-8"))
    return build, {"sourceSettingsSha256": hashlib.sha256(original).hexdigest(),
                   "settingsOverlaySha256": hashlib.sha256(SETTINGS_OVERLAY.encode()).hexdigest(),
                   "effectiveSettingsSha256": hashlib.sha256(settings.read_bytes()).hexdigest()}

def kvm_available():
    try:
        return stat.S_ISCHR(os.stat("/dev/kvm").st_mode) and os.access("/dev/kvm", os.R_OK | os.W_OK, effective_ids=True)
    except OSError:
        return False

def command(args, env, timeout=120, data=None, capture=False):
    process = subprocess.Popen(args, cwd=ROOT, env=env,
                               stdin=subprocess.DEVNULL if data is None else subprocess.PIPE,
                               stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = process.communicate(input=data, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        raise
    require(process.returncode == 0, "command_failed")
    if capture:
        require(len(output) <= 65536, "output_bound_exceeded")
        return output

def process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19], Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")

def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n")
    temporary.replace(path)

def setup_environment(env, private):
    result = dict(env)
    result.update(HOME=str(private / "home"), ANDROID_USER_HOME=str(private / "android"),
                  ANDROID_AVD_HOME=str(private / "android/avd"), GRADLE_USER_HOME=str(private / "gradle"),
                  TMPDIR=str(private / "tmp"), ANDROID_SERIAL=SERIAL,
                  REPROOF_OWNED_ANDROID_EMULATOR="1", JAVA_HOME=env["JAVA_HOME_17_X64"])
    result["PATH"] = ":".join((result["JAVA_HOME"] + "/bin", env["ANDROID_HOME"] + "/platform-tools",
                                   env["ANDROID_HOME"] + "/emulator", env.get("PATH", "")))
    return result

def cleanup(private, evidence, name, env, child=None):
    """Never claim success when process identity or termination is uncertain."""
    report = {"emulator": False, "reaped": False, "ownedAvd": False, "privateHomes": False}
    if not private.exists():
        return {"confirmed": True, "nothingCreated": True}
    require(not private.is_symlink(), "cleanup_path_rejected")
    state_path = private / "state.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    require(state.get("name", name) == name, "cleanup_identity_rejected")
    pid = state.get("pid")
    try:
        if pid is None:
            require(not state.get("spawnPending"), "emulator_identity_uncertain")
            report["emulator"] = report["reaped"] = True
        else:
            require(type(pid) is int and pid > 1, "emulator_identity_uncertain")
            try:
                start, argv = process_identity(pid)
            except FileNotFoundError:
                if child is not None:
                    child.wait(timeout=1)
                report["reaped"] = True
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    report["emulator"] = True
            else:
                require(start == state.get("start") and name.encode() in argv
                        and b"-avd" in argv and os.getpgid(pid) == pid, "emulator_identity_uncertain")
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if child is not None and child.poll() is not None:
                        break
                    if not Path(f"/proc/{pid}").exists():
                        break
                    time.sleep(.1)
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if child is not None:
                    child.wait(timeout=5)
                    report["reaped"] = True
                else:
                    report["reaped"] = not Path(f"/proc/{pid}").exists()
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    report["emulator"] = report["reaped"]
    except Exception:
        pass
    try:
        if report["emulator"]:
            avd = private / "android/avd"
            if avd.exists():
                shutil.rmtree(avd)
            report["ownedAvd"] = not avd.exists()
    except Exception:
        pass
    try:
        if report["emulator"] and report["ownedAvd"]:
            shutil.rmtree(private)
            report["privateHomes"] = not private.exists()
    except Exception:
        pass
    report["confirmed"] = all(report.values())
    return report

def run(env, temp, name, private, evidence):
    verify_source()
    require(not private.exists() and not evidence.exists(), "fresh_run_required")
    if not kvm_available():
        evidence.mkdir(mode=0o700)
        save(evidence / "supervisor.json", {
            "outcome": "stopped", "code": "existing_kvm_access_required",
            "cleanup": {"confirmed": True, "nothingCreated": True}, "nativeInputs": 0, "aclChanges": False})
        return 1
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    state = {"name": name, "spawnPending": False}
    save(private / "state.json", state)
    child = None
    outcome, code = "failed", "unclassified_failure"
    clean = {"confirmed": False}
    build_provenance = {}
    local = dict(env)
    try:
        local = setup_environment(env, private)
        for key in ("HOME", "ANDROID_USER_HOME", "ANDROID_AVD_HOME", "GRADLE_USER_HOME", "TMPDIR"):
            Path(local[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
        sdk = Path(local["ANDROID_HOME"])
        require((Path(local["JAVA_HOME"]) / "bin/java").is_file()
                and (sdk / "platforms/android-35/android.jar").is_file()
                and (sdk / "build-tools/35.0.0").is_dir(), "toolchain_missing")
        require(hashlib.sha256((ROOT / "android/gradle/wrapper/gradle-wrapper.jar").read_bytes()).hexdigest() == WRAPPER,
                "wrapper_rejected")
        devices = command([str(sdk / "platform-tools/adb"), "devices"], local, capture=True).decode().splitlines()[1:]
        require(not any(line.strip() for line in devices), "existing_device_rejected")
        build, build_provenance = prepare_build(private)
        command([str(build / "gradlew"), "-p", str(build), "--no-daemon", "--no-build-cache",
                 "--console=plain", "-Duser.home=" + local["HOME"], ":live:assembleDebug", ":runtime-fixture:assembleDebug"],
                local, timeout=600)
        command([str(sdk / "cmdline-tools/latest/bin/sdkmanager"), "--install", "emulator", IMAGE], local, timeout=300)
        command([str(sdk / "cmdline-tools/latest/bin/avdmanager"), "create", "avd", "--name", name,
                 "--package", IMAGE, "--device", "pixel_2"], local, data=b"no\n")
        state["spawnPending"] = True
        save(private / "state.json", state)
        child = subprocess.Popen([str(sdk / "emulator/emulator"), "-avd", name, "-port", "5554", "-no-window",
                                  "-no-audio", "-no-snapshot", "-no-metrics", "-no-boot-anim", "-gpu",
                                  "swiftshader_indirect", "-accel", "on"], env=local, cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        state.update(pid=child.pid, start=process_identity(child.pid)[0], spawnPending=False)
        save(private / "state.json", state)
        deadline = time.monotonic() + 240
        while True:
            require(child.poll() is None, "emulator_stopped")
            try:
                booted = command([str(sdk / "platform-tools/adb"), "-s", SERIAL, "shell", "getprop", "sys.boot_completed"],
                                 local, timeout=5, capture=True).strip() == b"1"
            except (Stop, subprocess.TimeoutExpired):
                booted = False
            if booted:
                break
            require(time.monotonic() < deadline, "boot_timeout")
            time.sleep(2)
        command([sys.executable, str(ROOT / "scripts/android-startup-one-shot-qa.py"), "--serial", SERIAL,
                 "--owned-emulator-name", name, "--helper-apk", str(build / "live/build/outputs/apk/debug/live-debug.apk"),
                 "--app-apk", str(build / "runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk"),
                 "--output", str(evidence / "android")], local, timeout=120)
        outcome, code = "passed", "startup_checked"
    except Stop as error:
        code = str(error)
    except BaseException:
        code = "unclassified_failure"
    finally:
        clean = cleanup(private, evidence, name, local, child)
        if not clean["confirmed"]:
            outcome = "failed"
        save(evidence / "supervisor.json", {"outcome": outcome, "code": code, "cleanup": clean,
                                           "nativeInputs": 0, "aclChanges": False, "buildSettings": build_provenance})
    return 0 if outcome == "passed" and clean["confirmed"] else 1

def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        require(args in (["run"], ["cleanup"]), "mode_rejected")
        env = dict(os.environ)
        temp, name = guard(env)
        private = temp / ("reproof-startup-private-" + name)
        evidence = temp / "reproof-startup-evidence"
        if args == ["cleanup"]:
            report = cleanup(private, evidence, name, env)
            if evidence.is_dir() and not evidence.is_symlink():
                summary_path = evidence / "supervisor.json"
                summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {"outcome": "stopped"}
                summary["fallbackCleanup"] = report
                save(summary_path, summary)
            return 0 if report["confirmed"] else 1
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(Stop("terminated")))
        signal.signal(signal.SIGINT, lambda *_: (_ for _ in ()).throw(Stop("interrupted")))
        return run(env, temp, name, private, evidence)
    except BaseException:
        print('{"outcome":"stopped","code":"preflight_or_cleanup_failed"}')
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
