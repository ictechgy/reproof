#!/usr/bin/env python3
"""Reconstructed local proposal: one authorized CI attempt; no ACL remediation.

Subprocess output is discarded except bounded machine checks. Cleanup is not a
second runtime attempt. This reconstruction requires fresh independent review.
"""
from __future__ import annotations
import hashlib
import importlib.util
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
_acl_spec = importlib.util.spec_from_file_location("android_kvm_acl", ROOT / "scripts/ci/android-kvm-acl.py")
acl = importlib.util.module_from_spec(_acl_spec)
_acl_spec.loader.exec_module(acl)
BRANCH = "codex/android-startup-kvm-one-shot-validation"
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
        acl.canonical_identity()
        return os.access("/dev/kvm", os.R_OK | os.W_OK, effective_ids=True)
    except (OSError, acl.ACLStop):
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
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

def setup_environment(env, private):
    result = dict(env)
    result.update(HOME=str(private / "home"), ANDROID_USER_HOME=str(private / "android"),
                  ANDROID_AVD_HOME=str(private / "android/avd"), GRADLE_USER_HOME=str(private / "gradle"),
                  TMPDIR=str(private / "tmp"), ANDROID_SERIAL=SERIAL,
                  REPROOF_OWNED_ANDROID_EMULATOR="1", JAVA_HOME=env["JAVA_HOME_17_X64"])
    result["PATH"] = ":".join((result["JAVA_HOME"] + "/bin", env["ANDROID_HOME"] + "/platform-tools",
                                   env["ANDROID_HOME"] + "/emulator", env.get("PATH", "")))
    return result

def acl_requirement(evidence):
    marker = evidence / "acl-transaction.json"
    if not marker.exists() and not marker.is_symlink():
        return None
    require(not marker.is_symlink() and marker.is_file() and marker.stat().st_size <= 128, "acl_marker_rejected")
    value = json.loads(marker.read_text())
    require(type(value) is dict and set(value) == {"required", "restored"}
            and value["required"] is True and type(value["restored"]) is bool, "acl_marker_rejected")
    return value

def cleanup(private, evidence, name, env, child=None, acl_backend=None):
    """Never claim success when process identity or termination is uncertain."""
    report = {"emulator": False, "reaped": False, "ownedAvd": False, "privateHomes": False, "aclRestored": False, "aclDescriptorClosed": acl_backend is None}
    requirement = None
    marker_valid = True
    try:
        requirement = acl_requirement(evidence)
    except BaseException:
        marker_valid = False
    if not private.exists() or private.is_symlink():
        if acl_backend is None and not private.is_symlink() and marker_valid:
            if not evidence.exists():
                return {"confirmed": True, "nothingCreated": True}
            try:
                summary_path = evidence / "supervisor.json"
                require(not summary_path.is_symlink() and summary_path.stat().st_size <= 65536, "summary_rejected")
                summary = json.loads(summary_path.read_text())
                if summary.get("cleanup", {}).get("confirmed") is True and (requirement is None or requirement["restored"]):
                    return {"confirmed": True, "previousCleanupVerified": True}
            except BaseException:
                pass
        if acl_backend is not None:
            try:
                acl_backend.close()
                report["aclDescriptorClosed"] = True
            except BaseException:
                pass
        report["confirmed"] = False
        return report
    state = {}
    try:
        state_path = private / "state.json"
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        require(state.get("name", name) == name, "cleanup_identity_rejected")
        pid = state.get("pid")
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
        acl_state = private / "kvm-acl"
        required = acl_backend is not None or requirement is not None or state.get("aclRequired") is True
        require(marker_valid, "acl_marker_rejected")
        if not acl_state.exists():
            report["aclRestored"] = not required
        else:
            report["aclRestored"] = acl.restore(acl_state, backend=acl_backend)["restored"] is True
        if report["aclRestored"] and requirement is not None:
            save(evidence / "acl-transaction.json", {"required": True, "restored": True})
    except BaseException:
        report["aclRestored"] = False
    finally:
        if acl_backend is not None:
            try:
                acl_backend.close()
                report["aclDescriptorClosed"] = True
            except BaseException:
                report["aclDescriptorClosed"] = False
    try:
        if report["emulator"]:
            avd = private / "android/avd"
            if avd.exists():
                shutil.rmtree(avd)
            report["ownedAvd"] = not avd.exists()
    except Exception:
        pass
    try:
        if report["emulator"] and report["ownedAvd"] and report["aclRestored"] and report["aclDescriptorClosed"]:
            shutil.rmtree(private)
            report["privateHomes"] = not private.exists()
    except Exception:
        pass
    report["confirmed"] = all(report.values())
    return report

def run(env, temp, name, private, evidence):
    verify_source()
    require(not private.exists() and not evidence.exists(), "fresh_run_required")
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    state = {"name": name, "spawnPending": False, "aclRequired": False}
    save(private / "state.json", state)
    child = None
    outcome, code = "failed", "unclassified_failure"
    clean = {"confirmed": False}
    build_provenance = {}
    acl_grant_attempted = acl_grant_verified = False
    acl_backend = None
    local = dict(env)
    try:
        if not kvm_available():
            try:
                state["aclRequired"] = True
                save(private / "state.json", state)
                save(evidence / "acl-transaction.json", {"required": True, "restored": False})
                (private / "kvm-acl").mkdir(mode=0o700)
                acl_backend = acl.NativeBackend()
                acl.prepare(private / "kvm-acl", backend=acl_backend)
                acl_grant_attempted = True
                acl.grant(private / "kvm-acl", backend=acl_backend)
                require(kvm_available(), "kvm_acl_access_unconfirmed")
                acl_grant_verified = True
            except Exception:
                raise Stop("kvm_acl_rejected") from None
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
        clean = cleanup(private, evidence, name, local, child, acl_backend)
        if not clean["confirmed"]:
            outcome = "failed"
        save(evidence / "supervisor.json", {"outcome": outcome, "code": code, "cleanup": clean,
                                           "nativeInputs": 0, "aclGrantAttempted": acl_grant_attempted,
                                           "aclGrantVerified": acl_grant_verified, "buildSettings": build_provenance})
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
