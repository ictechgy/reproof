#!/usr/bin/env python3
"""Execute the shared issue lifecycle against owned local processes and MP4.

This is an acceptance driver, not a summary of unit-test counts. A synthetic
PASS qualifies these software boundaries on one Mac, never company hardware.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reproloop import contracts
from reproloop.live.access import AccessStore
from reproloop.live.client import IssueClient
from tests.fixtures.g7_qa_runtime import call_backend, collection_policy, project_document


class GateFailure(RuntimeError): pass


def require(condition, code):
    if not condition: raise GateFailure(code)


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def sources():
    paths = [*ROOT.glob("reproloop/**/*.py"), *ROOT.glob("live-web/*.js"),
             *ROOT.glob("live-web/*.html"), *ROOT.glob("live-web/*.css"),
             *ROOT.glob("native/macos-video/**/*.swift"), ROOT / "native/macos-media-validator/main.swift",
             Path(__file__), ROOT / "scripts/verify-video.py", *ROOT.glob("tests/**/*.py"),
             *ROOT.glob("tests/fixtures/media/*")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths)) if path.is_file()}


class Process:
    def __init__(self, kind, configuration):
        self.kind = kind
        self.process = subprocess.Popen([sys.executable, str(ROOT / "tests/fixtures/g7_qa_runtime.py"), kind],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.send(configuration)
        if kind != "coordinator": self.process.stdin.close()

    def send(self, value):
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def read(self, timeout=20):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            require(bool(selector.select(timeout)), self.kind + "_startup_timeout")
        line = self.process.stdout.readline(16 * 1024 + 1)
        require(0 < len(line) <= 16 * 1024, self.kind + "_startup_failed")
        value = json.loads(line)
        require("error" not in value, self.kind + "_configuration_failed")
        return value

    def stop(self):
        if self.process.poll() is None:
            if self.kind == "coordinator":
                try: self.send({"operation": "stop"}); self.process.stdin.close()
                except (OSError, ValueError): pass
            else: self.process.send_signal(signal.SIGINT)
            try: self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=3)
        return self.process.returncode

    def close(self):
        code = self.stop()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed: stream.close()
        return code


def wait_for(read, predicate, *, seconds=30, code="expected_state_timeout"):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = read()
        if predicate(last): return last
        time.sleep(.1)
    raise GateFailure(code)


def runtime_configuration(project, backend):
    return {"schemaVersion": 1, "kind": "reproloop-issue-runtime", "projects": [{
        "projectId": project["id"], "projectDigest": contracts.digest(project),
        "runtimePolicy": {"schemaVersion": 1, "class": "mobile-device", "network": "loopback",
            "candidateCanEdit": False, "enforcedControls": ["host_authority"], "attestations": []},
        "validationRecipeIds": ["regression_ui"],
        "fixtures": [{"applicationId": "ios_app", "fixtureId": "seed_account", "endpointId": "fixture_service",
            "baseUrl": backend, "checkRecipeIds": ["check_account"], "cleanupRecipeId": "cleanup_account", "payload": {}}],
        "variables": [], "observations": [{"observationId": "screen", "providerIncarnation": "owned_counter_backend",
            "baseUrl": backend, "coverage": ["snapshot"]}]}]}


def authored_specification(view):
    actions = [{"eventId": event["id"], **copy.deepcopy(event["input"])}
               for event in view["recording"]["original"]["events"] if "input" in event]
    require(len(actions) == 1, "original_input_count_changed")
    assertions = []
    for role, count in (("defect", 2), ("expected", 1)):
        assertions.append({"id": role + "_counter", "role": role,
            "predicate": {"kind": "property", "observationId": "screen", "property": "count", "operator": "equals", "value": count},
            "coverage": {"class": "snapshot", "windowMs": {"start": 0, "end": 0}, "maxUncertaintyMs": 1000,
                "maxAgeMs": 2000, "scope": "root", "properties": ["count"]}, "windowMs": 0, "stabilityMs": 0})
    return {"baseRevision": 0, "actions": actions, "waits": [], "bindings": [],
            "fixtures": ["seed_account"], "assertions": assertions}


def native_descriptor(path, output):
    supplied = Path(path)
    required = {"coordinatorOrigin", "projectId", "applicationId", "buildId", "deviceIds", "preparationIds", "ownedResources"}
    if not supplied.is_file() or supplied.is_symlink() or supplied.stat().st_size > 64 * 1024:
        result = {"status": "blocked-unqualified", "reason": "native_descriptor_unavailable", "requiredFields": sorted(required)}
    else:
        document = json.loads(supplied.read_text())
        require(type(document) is dict and not (set(document) - required - {"schemaVersion"}), "native_descriptor_fields")
        missing = sorted(required - set(document))
        if missing:
            result = {"status": "blocked-unqualified", "reason": "native_descriptor_incomplete", "missingFields": missing}
        else:
            require(type(document.get("schemaVersion")) is int and document["schemaVersion"] == 1
                    and document["ownedResources"] is True, "native_descriptor_ownership")
            for field in ("projectId", "applicationId", "buildId"): contracts.validate_id(document[field])
            require(type(document["deviceIds"]) is list and len(document["deviceIds"]) == 2
                    and len(set(document["deviceIds"])) == 2, "native_descriptor_devices")
            require(type(document["preparationIds"]) is list and 1 <= len(document["preparationIds"]) <= 8,
                    "native_descriptor_preparations")
            for identifier in document["deviceIds"] + document["preparationIds"]: contracts.validate_id(identifier)
            origin = urlsplit(document["coordinatorOrigin"])
            require(origin.scheme == "https" and origin.hostname and origin.port and not origin.username and not origin.password
                    and origin.path in {"", "/"} and not origin.query and not origin.fragment, "native_descriptor_origin")
            result = {"status": "blocked-unqualified", "reason": "native_access_and_host_measurements_required",
                "descriptorValidated": True, "requiredInputs": ["authorized personal credential via stdin",
                    "distinct measured Mac host identities", "compatible registered devices and company fixture service"]}
    result.update(environment="native", physicalDeviceAcceptance=False, twoMacAcceptance=False)
    save(output / "result.json", result)
    return 2


def run_synthetic(args, output):
    before = sources()
    steps = []; processes = []; result = {"schemaVersion": 1, "environment": "synthetic-local",
        "sameMac": True, "syntheticAdapter": True, "physicalDeviceAcceptance": False,
        "twoMacAcceptance": False, "companyApplicationAcceptance": False, "steps": steps, "status": "running"}
    step = "compile"
    started = time.monotonic()
    def passed(name, **evidence):
        steps.append({"step": name, "status": "passed", **evidence})
        print(json.dumps({"step": name, "status": "passed"}), flush=True)
    with tempfile.TemporaryDirectory(prefix="repro-g7-owned-") as temporary:
        root = Path(temporary)
        access = None
        try:
            module_spec = importlib.util.spec_from_file_location("g7_video_compile", ROOT / "scripts/verify-video.py")
            compiler = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(compiler)
            build = output / "native"; build.mkdir()
            helper, compilation = compiler.compile_helper(build)
            media = build / "media-validator"
            code, stdout, stderr = compiler.run_bounded(["/usr/bin/xcrun", "swiftc", str(ROOT / "native/macos-media-validator/main.swift"), "-o", str(media)],
                cwd=ROOT, timeout=45)
            require(code == 0, "media_helper_compile_failed")
            save(build / "compilation.json", {"encoder": compilation,
                "validatorSha256": hashlib.sha256(media.read_bytes()).hexdigest(), "validatorStdoutBytes": len(stdout), "validatorStderrBytes": len(stderr)})
            passed("compile", actualEncoding="avfoundation-h264-mp4")

            step = "start-backend"
            project = project_document(); policy = collection_policy()
            backend = Process("backend", {"projectDigest": contracts.digest(project)}); processes.append(backend)
            backend_public = backend.read(); backend_origin = backend_public["backend"]
            configuration_path = root / "issue-runtime.json"
            save(configuration_path, runtime_configuration(project, backend_origin))
            coordinator_root = root / "coordinator"; coordinator_root.mkdir()
            access = AccessStore(coordinator_root / "coordinator-v2")
            access.bootstrap_administrator("admin"); access.register_project("admin", project)
            access.create_identity("admin", "qa_owner")
            for role in ("operator", "maintainer", "viewer"): access.grant_membership("admin", "checkout", "qa_owner", role)
            credential = access.issue_principal_credential("admin", "qa_owner", lifetime_seconds=1800)["token"]
            coordinator_config = {"root": str(coordinator_root), "project": project, "collectionPolicy": policy,
                "issueConfiguration": str(configuration_path), "videoHelper": str(helper), "mediaHelper": str(media)}
            step = "start-coordinator"
            coordinator = Process("coordinator", coordinator_config); processes.append(coordinator)
            coordinator_public = coordinator.read(); origin = coordinator_public["coordinator"]
            coordinator_config["port"] = urlsplit(origin).port
            workers = []; worker_public = []
            for number in ("one", "two"):
                step = "start-worker-" + number
                host = "worker-" + number
                enrollment = access.create_host_enrollment("admin", host_id=host, project_ids=["checkout"], trust_groups=[],
                    lifetime_seconds=300, credential_lifetime_seconds=1800)
                transport = secrets.token_urlsafe(40)
                worker_root = root / host
                process = Process("worker", {"root": str(worker_root), "coordinator": origin,
                    "enrollmentToken": enrollment["token"], "transportToken": transport, "hostId": host,
                    "incarnation": "owned-boot-" + number, "deviceAlias": "phone", "physicalId": "owned-synthetic-" + secrets.token_hex(16),
                    "project": project, "collectionPolicy": policy, "backend": backend_origin})
                processes.append(process); public = process.read(); worker_public.append(public)
                require(public["actualWorkerCli"] is True and public["twoMacAcceptance"] is False, "worker_truthfulness")
                # Only this run's newly created private file is read; it lives in
                # the temporary directory and is never copied into evidence.
                private = json.loads((worker_root / "owned-host-credential.json").read_text())
                access.assign_device("admin", host + "--phone", project_id="checkout", host_id=host)
                workers.append({"id": host, "url": public["worker"], "token": transport, "hostCredential": private["credential"]})
            step = "install-workers"
            coordinator.send({"operation": "install-workers", "workers": workers})
            require(len(coordinator.read()["installed"]) == 2, "two_workers_not_installed")
            step = "personal-login"
            client = IssueClient(origin, credential)
            step = "inventory-catalog"
            catalog = client.call("/api/release/projects")
            save(output / "catalog.json", catalog)
            devices = catalog["projects"][0]["devices"]
            require(len(devices) == 2 and all(d["state"] == "available" for d in devices), "inventory_not_available")
            passed("processes-and-inventory", coordinatorPid=coordinator_public["pid"], backendPid=backend_public["pid"],
                workerPids=[p.process.pid for p in processes if p.kind == "worker"], workerCli=True, availableDevices=2)

            step = "prepare-and-record"
            prefix = "/api/release/issues/"
            selected = client.call("/api/release/issues", {"projectId": "checkout", "applicationId": "ios_app", "buildId": "original",
                "deviceId": "worker-one--phone", "clientId": "gate_recorder", "preparationIds": ["seed_account"], "unprepared": False})
            issue_id = selected["issue"]["id"]
            read = lambda: client.call(prefix + issue_id)
            active = wait_for(read, lambda v: v["issue"]["state"] not in {"preparing"}, seconds=35)
            save(output / "recording-start.json", active)
            require(active["issue"]["state"] == "recording", "prepared_recording_failed")
            session_path = "/api/sessions/" + active["issue"]["sessionId"]
            session = client.call(session_path)["session"]
            # Use current geometry from the actual authenticated frame route.
            frame = client.call(session_path + "/frame")
            typed = {"action": "tap", "parameters": {"x": .5, "y": .5}, "geometry": {
                "width": frame["width"], "height": frame["height"], "rotation": 0 if frame["orientation"] == "portrait" else 90,
                "version": frame["geometryVersion"]}}
            client.call(prefix + issue_id + "/input", {"input": typed, "operationId": "gate_manual_tap", "sequence": 1,
                "controllerId": session["controllerId"], "epoch": session["epoch"]})
            observed = call_backend(backend_origin, "/snapshot")
            require(observed["count"] == 2 and len(observed["inputs"]) == 1, "actual_application_input_not_observed")
            # Real elapsed capture, including a geometry transition and irregular cadence.
            time.sleep(3)
            client.call(prefix + issue_id + "/stop", {})
            frozen = wait_for(read, lambda v: v["issue"]["state"] not in {"preparing", "recording", "finalizing"}, seconds=45)
            save(output / "original-issue.json", frozen)
            require(frozen["lifecycle"]["deviceCleanup"] == "complete"
                    and all(item["status"] == "complete" for item in frozen["lifecycle"]["cleanup"]), "original_cleanup_failed")
            require(frozen["recording"]["status"] == "frozen-incomplete", "remote_timing_unknown_was_hidden")
            require(frozen["video"] and len(frozen["video"]["segments"]) >= 2, "actual_rotated_video_missing")
            require(all(segment["captureInterval"] is None for segment in frozen["video"]["segments"]), "remote_capture_time_was_invented")
            passed("prepare-input-video-stop", originalDigest=frozen["recording"]["recordingDigest"],
                immutableStatus=frozen["recording"]["status"], segments=len(frozen["video"]["segments"]), actualInputCount=1)

            step = "save-specification"
            saved = client.call(prefix + issue_id + "/specifications", authored_specification(frozen))
            save(output / "saved-specification.json", saved)
            digest = saved["specificationDigest"]
            require(digest == contracts.digest(saved["specification"]), "specification_digest_changed")
            passed("save-specification", specificationDigest=digest)

            step = "restart-coordinator"
            old_pid = coordinator.process.pid
            require(coordinator.stop() == 0, "coordinator_shutdown_failed")
            coordinator = Process("coordinator", coordinator_config); processes.append(coordinator)
            restarted = coordinator.read()
            require(restarted["coordinator"] == origin and restarted["pid"] != old_pid, "coordinator_process_not_restarted")
            coordinator.send({"operation": "install-workers", "workers": workers}); coordinator.read()
            client = IssueClient(origin, credential)
            retained = client.call(prefix + issue_id)
            require(retained["recording"]["original"] == frozen["recording"]["original"]
                    and retained["specification"] == saved["specification"], "restart_changed_immutable_evidence")
            passed("restart-coordinator", previousPid=old_pid, restartedPid=restarted["pid"], immutableOriginalPreserved=True)

            step = "export-import"
            media_output = output / "media"; media_output.mkdir()
            for index, segment in enumerate(retained["video"]["segments"]):
                with client._request(prefix + issue_id + "/media/" + segment["digest"],
                    headers={"Range": f'bytes=0-{segment["bytes"] - 1}'}) as response:
                    body = response.read(segment["bytes"] + 1)
                    require(response.status == 206 and len(body) == segment["bytes"]
                        and hashlib.sha256(body).hexdigest() == segment["digest"], "retained_video_range_changed")
                (media_output / f"segment-{index + 1}.mp4").write_bytes(body)
            archive = output / "issue.zip"
            exported = client.export_package(issue_id, archive)
            imported = client.import_package("checkout", archive)
            imported_id = imported["issue"]["id"]
            received = client.call(prefix + imported_id)
            require(received["recording"]["original"] == frozen["recording"]["original"]
                    and received["specification"] == saved["specification"] and received["approval"] is None,
                    "import_changed_specification_or_trusted_approval")
            for index, segment in enumerate(received["video"]["segments"]):
                with client._request(prefix + imported_id + "/media/" + segment["digest"],
                    headers={"Range": f'bytes=0-{segment["bytes"] - 1}'}) as response:
                    body = response.read(segment["bytes"] + 1)
                    require(response.status == 206 and len(body) == segment["bytes"]
                        and hashlib.sha256(body).hexdigest() == segment["digest"], "retained_video_range_changed")
                require(body == (media_output / f"segment-{index + 1}.mp4").read_bytes(), "imported_mp4_changed")
            save(output / "imported-issue.json", received)
            passed("export-import", archiveDigest=exported["package"]["archiveDigest"],
                originalPreserved=True, specificationPreserved=True, localApprovalRequired=True)

            step = "approve-and-replay"
            client.call(prefix + imported_id + "/approve", {"revision": saved["specification"]["revision"],
                "specificationDigest": digest, "bindImported": True})
            replay_body = {"deviceId": "worker-two--phone", "clientId": "gate_replayer", "specificationDigest": digest}
            wait_for(lambda: client.call("/api/release/projects"),
                lambda v: all(d["state"] == "available" for d in v["projects"][0]["devices"]), seconds=20)
            client.call(prefix + imported_id + "/replay", replay_body)
            final = wait_for(lambda: client.call(prefix + imported_id),
                lambda v: v["issue"]["state"] not in {"replaying", "cancelling"}, seconds=90)
            save(output / "replay-result.json", final)
            state = call_backend(backend_origin, "/snapshot")
            save(output / "backend-observations.json", state)
            require(final["issue"]["state"] == "reproduced", "approved_original_did_not_reproduce")
            require(final["specificationDigest"] == digest and len(final["campaign"]["attempts"]) == 3, "frozen_attempt_budget_changed")
            require(len(state["inputs"]) == 4 and not state["prepared"] and len(state["observations"]) == 3,
                "actual_replay_effects_or_cleanup_changed")
            repeated = client.call(prefix + imported_id + "/replay", replay_body)
            require(repeated["campaign"] == final["campaign"] and len(call_backend(backend_origin, "/snapshot")["inputs"]) == 4,
                    "replay_retry_reset_budget")
            final_catalog = wait_for(lambda: client.call("/api/release/projects"),
                lambda v: all(d["state"] == "available" for d in v["projects"][0]["devices"]), seconds=20)
            save(output / "final-catalog.json", final_catalog)
            passed("approved-replay-on-second-worker", attempts=3, actualInputs=3, observations=3, predicateEvaluations=6,
                defect="count equals 2", expected="count equals 1", fixtureCleanup="complete", deviceCleanup="complete", budgetRetryStable=True)
            if args.browser:
                step = "browser"
                from tests.fixtures.g7_browser_check import browser_check
                browser_result = browser_check(origin, credential, issue_id, imported_id, received, output,
                    revoke=lambda: [access.revoke_membership("admin", "checkout", "qa_owner", role)
                        for role in ("viewer", "operator", "maintainer")])
                save(output / "browser.json", browser_result)
                require(browser_result["passed"], "browser_acceptance_failed")
                after_browser = call_backend(backend_origin, "/snapshot")
                save(output / "backend-after-browser.json", after_browser)
                require(not after_browser["prepared"] and len(after_browser["inputs"]) == 8
                    and len(after_browser["observations"]) == 6, "browser_application_effects_or_cleanup_changed")
                passed("browser", **browser_result)
            result["status"] = "passed"
        except Exception as error:
            import traceback
            result.update(status="failed", failedStep=step, reason=getattr(error, "code", None) or
                (str(error) if isinstance(error, GateFailure) else type(error).__name__))
            result["failureFrames"] = [{"file": str(Path(frame.filename).relative_to(ROOT)) if Path(frame.filename).is_relative_to(ROOT) else Path(frame.filename).name,
                "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
            for name in ('startup-diagnostic.json', 'replay-diagnostic.json'):
                diagnostic = root / 'coordinator' / name
                if diagnostic.exists(): save(output / name, json.loads(diagnostic.read_text()))
            # Child tracebacks are retained only after redacting every credential
            # minted by this run; ordinary evidence never includes private setup.
            for owned in processes:
                if owned.process.poll() is not None:
                    raw = owned.process.stderr.read(16 * 1024)
                    for secret in [locals().get("credential"), *[w[key] for w in locals().get("workers", []) for key in ("token", "hostCredential")],
                                   locals().get("transport"), locals().get("enrollment", {}).get("token")]:
                        if secret: raw = raw.replace(secret, "[REDACTED]")
                    if raw: (output / (owned.kind + "-" + str(owned.process.pid) + "-error.log")).write_text(raw)
        finally:
            diagnostic = root / "coordinator" / "media-validation.json"
            if diagnostic.exists(): save(output / "media-validation.json", json.loads(diagnostic.read_text()))
            for worker_name in ('worker-one', 'worker-two'):
                diagnostic = root / worker_name / 'capture-diagnostic.json'
                if diagnostic.exists():
                    save(output / (worker_name + '-capture-diagnostic.json'), json.loads(diagnostic.read_text()))
            cleanup = [{"kind": p.kind, "pid": p.process.pid, "exitCode": p.close()} for p in reversed(processes)]
            if access is not None: access.close()
            result["processCleanup"] = cleanup
            if any(p["exitCode"] != 0 for p in cleanup):
                result.update(status="failed", cleanupReason="owned_process_exit_failed")
    after = sources()
    result.update(sourcesStable=before == after, sources=after, durationSeconds=round(time.monotonic() - started, 3))
    if before != after: result.update(status="failed", reason="sources_changed_during_gate")
    save(output / "result.json", result)
    print(json.dumps({key: result[key] for key in ("status", "environment", "sourcesStable")}), flush=True)
    return 0 if result["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, help="synthetic-local or an explicit owned native descriptor path")
    parser.add_argument("--output-new", required=True, type=Path, help="New evidence directory; existing paths are refused")
    parser.add_argument("--browser", action="store_true", help="Run the installed isolated agent-browser against actual MP4")
    args = parser.parse_args(argv)
    output = args.output_new.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    if args.environment != "synthetic-local": return native_descriptor(args.environment, output)
    return run_synthetic(args, output)


if __name__ == "__main__":
    raise SystemExit(main())
