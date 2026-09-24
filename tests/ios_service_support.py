"""Owned iOS service fixture for the persistent trusted mobile adapter.

The fixture composes the real operation journal, pinned XCTest runner, retained
Lab scope, scenario registry, and issue session service.  Only the XCTest
binary, CoreDevice command, and helper HTTP endpoint are protocol doubles.
"""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import threading
import time
import unittest.mock
import zipfile

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.execution.journal import RunStore
from reproof.ios_device_tools import IOSDeviceQueryDefinition, IOSDeviceTools
from reproof.ios_mobile_inputs import IOSBaselineReference, IOSMobileInputsConfig
from reproof.ios_mobile_operation import IOSMobileOperationStore
from reproof.ios_profile import validate_ios_profile
from reproof.ios_sanitation import validate_ios_sanitation_policy
from reproof.ios_storage import tree_manifest
from reproof.live.authority import HostAuthority
from reproof.live.clock_sync import ClockSynchronizer
from reproof.live.issue_sessions import FixturePreparation
from reproof.live.model import Lab
from reproof.qualification import ApprovedExecution
from reproof.repair_mobile import MobileContext
from tests import g4_support
from tests import test_ios_artifact_transfer as artifact_fixtures
from tests import test_ios_mobile_helper as helper_fixtures
from tests import test_ios_mobile_xctest as xctest_fixtures
from tests.test_clock_sync import FakeClock
from tests.test_ios_observation import ordinary_project
from tests.test_worker_profiles import physical_ios_document


def sanitation_document():
    return {
        "schemaVersion": 1,
        "kind": "ios-app-owned-sanitation",
        "paths": [{"root": "documents", "relativePath": "Drafts/Temporary"}],
        "userDefaultsKeys": ["draft.name"],
        "keychainGenericPasswords": [{"service": "io.example.debug", "account": "synthetic-user"}],
    }


def _rewrite_ipa(path, *, bundle, build_id, profile, sanitation):
    entries = {}
    infos = {}
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            entries[entry.filename] = archive.read(entry)
            infos[entry.filename] = entry
    info_name = next(name for name in entries if name.endswith("/Info.plist")
                     and "/Frameworks/" not in name and "/PlugIns/" not in name)
    info = plistlib.loads(entries[info_name])
    info.update(
        CFBundleIdentifier=bundle,
        CFBundlePackageType="APPL",
        CFBundleVersion=build_id,
        ReproAutoProfile=profile.data,
        ReproAutoProfileDigest=profile.digest,
        ReproBuildID=build_id,
        ReproRuntimeIdentitySchemaVersion=2,
        ReproSanitationPolicy=sanitation.data,
        ReproSanitationPolicyDigest=sanitation.digest,
    )
    entries[info_name] = plistlib.dumps(info)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(infos[name], body)
    path.chmod(0o600)


def _embed_app(app, *, bundle, build_id, metadata):
    info_path = Path(app) / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    # Consume the official preparer's capability metadata. A fixture-local
    # schema override previously masked mismatched preparation/launch versions.
    info.update({key: value for key, value in metadata.items() if key.startswith("Repro")})
    info.update(CFBundleIdentifier=bundle, CFBundlePackageType="APPL",
                CFBundleVersion=build_id, ReproBuildID=build_id)
    info_path.write_bytes(plistlib.dumps(info))
    info_path.chmod(0o600)


def _mark_ipa_app(path):
    entries = {}
    infos = {}
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            entries[entry.filename] = archive.read(entry)
            infos[entry.filename] = entry
    info_name = next(name for name in entries if name.endswith("/Info.plist")
                     and "/Frameworks/" not in name and "/PlugIns/" not in name)
    info = plistlib.loads(entries[info_name])
    info["CFBundlePackageType"] = "APPL"
    entries[info_name] = plistlib.dumps(info)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(infos[name], body)
    path.chmod(0o600)


def _project(original_tree_digest, candidate_archive_digest):
    project = g4_support.project_document()
    project["applications"][0].update(bundle="com.example.app")
    project["builds"][0]["artifactDigest"] = original_tree_digest
    project["builds"][1]["artifactDigest"] = candidate_archive_digest
    return project


def coordinate_specification(original):
    value = g4_support.specification(original)
    value["actions"] = [{"eventId": "event_1", "action": "tap",
                          "parameters": {"x": 0.5, "y": 0.5},
                          "geometry": {"width": 400, "height": 800,
                                        "rotation": 0, "version": 1}}]
    value["bindings"] = []
    return value


class SanitationHTTPDouble(helper_fixtures.OwnedHTTPDouble):
    def __init__(self, *args, stage_path, mode_path, cancel_event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stage_path = Path(stage_path)
        self.mode_path = Path(mode_path)
        self.cancel_event = cancel_event

    def _status(self):
        value = super()._status()
        value["helperIncarnation"] = self.launch._runner.native_owner.helper_incarnation
        value["nativeTimeMs"] = int(time.monotonic() * 1000) - 100
        return value

    def call(self, path, body=None, timeout=5, binary=False):
        if path == "/command" and body and body.get("action") in {"cleanup", "authority_cleanup"}:
            self.stage_path.write_text("cleanup")
        if path == "/command" and body and body.get("action") == "tap" and self.cancel_event is not None:
            self.cancel_event.set()
        if path.startswith("/frames/after/"):
            cursor = int(path.rsplit("/", 1)[1])
            now = int(time.monotonic() * 1000) - 100
            frame = dict(self.frame, nativeFrameId=cursor + 1,
                         id="native-" + str(cursor + 1),
                         nativeTiming={"version": 1,
                                       "nativeClockId": "ios-mach-continuous",
                                       "nativeIncarnation": "native_helper_double",
                                       "captureStartMs": now,
                                       "captureEndMs": now})
            return json.dumps(frame, separators=(",", ":")).encode("utf-8")
        return super().call(path, body=body, timeout=timeout, binary=binary)


class IOSServiceFixture:
    """A reusable real-service fixture; callers own adapter construction."""

    def __init__(self, egress=None):
        self.temp = tempfile.TemporaryDirectory(prefix="repro-ios-service-")
        self.root = Path(self.temp.name).resolve()
        self.artifacts = artifact_fixtures.IOSArtifactTransferTests(methodName="runTest")
        self.artifacts.setUp()
        self._cleanups = [self.artifacts.doCleanups]

        bundle = "com.example.app"
        sanitation = validate_ios_sanitation_policy(sanitation_document())
        source = self.root / "auto-profile-source"
        auto_document = ordinary_project(source)
        auto_document["applicationId"] = bundle
        project_path = source / auto_document["project"] / "project.pbxproj"
        project_document = plistlib.loads(project_path.read_bytes())
        for obj in project_document["objects"].values():
            settings = obj.get("buildSettings", {})
            if "PRODUCT_BUNDLE_IDENTIFIER" in settings:
                settings["PRODUCT_BUNDLE_IDENTIFIER"] = bundle
        project_path.write_bytes(plistlib.dumps(project_document))
        from reproof.ios_instrumentation import validate_ios_auto_profile
        auto = validate_ios_auto_profile(auto_document)
        from reproof.ios_instrumentation import prepare_ios_instrumentation
        prepared = self.root / "prepared-observation"
        prepare_ios_instrumentation(source, prepared, profile=auto, sanitation_policy=sanitation)
        self.prepared_runtime_info = plistlib.loads(
            (prepared / "source/ReproofInstrumentation/Info.plist").read_bytes())

        original_app = self.artifacts.make_flat_app()
        _embed_app(original_app, bundle=bundle, build_id="original-27", metadata=self.prepared_runtime_info)
        original_tree = contracts.digest(tree_manifest(original_app))
        original_bytes = sum(path.stat().st_size for path in original_app.rglob("*")
                             if path.is_file())
        original_ipa = self.artifacts.make_ipa(original_app, self.root / "original.ipa")
        original_ipa.chmod(0o600)
        candidate_app = self.artifacts.make_flat_app()
        _embed_app(candidate_app, bundle=bundle, build_id="candidate-28", metadata=self.prepared_runtime_info)
        candidate_ipa = self.artifacts.make_ipa(candidate_app, self.root / "candidate.ipa")
        candidate_ipa.chmod(0o600)
        candidate_archive_digest = hashlib.sha256(candidate_ipa.read_bytes()).hexdigest()
        original_ipa_bytes = original_ipa.read_bytes()
        candidate_ipa_bytes = candidate_ipa.read_bytes()
        candidate_archive_digest = hashlib.sha256(candidate_ipa_bytes).hexdigest()

        project = _project(original_tree, candidate_archive_digest)
        with unittest.mock.patch.object(g4_support, "project_document", return_value=project):
            self.env = g4_support.G4Environment()
        self._cleanups.append(self.env.close)
        self.registration = self.env.registration
        self.plan = self.env.plan

        original_data = physical_ios_document(original_tree)
        original_data.update(projectId=project["id"], projectDigest=self.registration.project_digest,
                             bundle=bundle, launchTarget={"kind": "bundle", "value": bundle})
        original_data["artifact"].update(kind="ios-app", sha256=original_tree,
                                          bytes=original_bytes, bundleVersion="1.0", bundleBuild="original-27")
        self.original_profile = validate_ios_profile(original_data)
        candidate_data = json.loads(json.dumps(original_data))
        candidate_data["buildId"] = "candidate"
        candidate_data["artifact"].update(kind="ios-ipa", sha256=candidate_archive_digest,
                                           bytes=len(candidate_ipa_bytes), bundleBuild="candidate-28")
        self.candidate_profile = validate_ios_profile(candidate_data)

        # Record the original through the real issue service before turning the
        # fixture device into a physical authority-owned device.
        device = self.env.lab.devices["device"]
        device["capabilities"].update(
            applicationIdentity=self.original_profile.application_identity,
            applicationProfile=self.original_profile.data,
            applicationProfileDigest=self.original_profile.digest,
        )
        self.approved = self._approve(self.env.record_original())

        self.authority = HostAuthority(self.root / "authority.sqlite3",
                                       lease_directory=self.root / "leases")
        self._cleanups.append(self.authority.close)
        received = self.authority.clock_sync.sample()
        sent = self.authority.clock_sync.sample()
        mapping = self.authority.clock_sync.record_exchange(
            coordinator_clock_id="ios-service-coordinator",
            coordinator_send_ns=received.nanoseconds,
            host_received=received,
            host_sent=sent,
            coordinator_receive_ns=sent.nanoseconds,
            max_drift_ppm=0,
        )
        self.grant = self.authority.issue_parent_grant(
            mapping, grant_id="ios-service-grant", project_id=project["id"],
            controller_id="ios-service-controller", renewal_sequence=1,
            coordinator_deadline_ns=sent.nanoseconds + 600_000_000_000)
        self.env.lab.authority = self.authority
        self.env.lab.parent_grant = self.grant
        self.udid = "ios-service-" + contracts.digest(str(self.root))[:24]
        device.update(kind="ios-physical", _authority={"deviceKind": "ios-physical",
                     "physicalId": self.udid})
        device["capabilities"]["authorityMode"] = "shared-v2"

        # Reuse the pinned XCTest/Mach-O fixtures, but bind their query to the
        # physical Lab device and to the actual configured bundle.
        self.xctest = xctest_fixtures.IOSMobileXCTestTests(methodName="runTest")
        self.xctest.setUp()
        self._cleanups.append(self.xctest.doCleanups)
        self.xctest.g.c.selected = replace(self.xctest.g.c.selected, bundle_id=bundle,
                                           udid=self.udid)
        self.xctest.g.write_tool(self._tool_extra())
        query_tool = IOSDeviceTools(self.xctest.g.q.tool,
                                    hashlib.sha256(self.xctest.g.q.tool.read_bytes()).hexdigest())
        query_root = self.root / "query-work"
        query_root.mkdir(mode=0o700)
        from tests import test_ios_device_tools as device_tools
        self.query = IOSDeviceQueryDefinition(query_tool, device_tools.IDENTIFIER,
                                              self.udid, bundle, query_root,
                                              self.xctest.g.guardian())

        helper_entries = dict(self.xctest.baselines.entries)
        helper_paths = {}
        for role in ("helper-host", "helper-runner"):
            path = self.root / (role + ".ipa")
            path.write_bytes(helper_entries[role + ".ipa"])
            _mark_ipa_app(path)
            path.chmod(0o600)
            helper_paths[role] = path
        original_path = self.root / "original-baseline.ipa"
        original_path.write_bytes(original_ipa_bytes)
        original_path.chmod(0o600)
        self.baselines = (
            IOSBaselineReference("original", bundle, original_path,
                                 hashlib.sha256(original_ipa_bytes).hexdigest(), len(original_ipa_bytes)),
            IOSBaselineReference("helper-host", "io.reproof.live.host", helper_paths["helper-host"],
                                 hashlib.sha256(helper_paths["helper-host"].read_bytes()).hexdigest(),
                                 helper_paths["helper-host"].stat().st_size),
            IOSBaselineReference("helper-runner", self.xctest.tools.template.runner_bundle_identifier,
                                 helper_paths["helper-runner"],
                                 hashlib.sha256(helper_paths["helper-runner"].read_bytes()).hexdigest(),
                                 helper_paths["helper-runner"].stat().st_size),
        )
        self.config = IOSMobileInputsConfig(
            self.env.lab, self.env.service, self.registration, "device", "repair",
            self.original_profile, self.query, self.baselines,
            tuple(self.env.preparations()), contracts.digest(g4_support.runtime_policy()),
            replace(self.xctest.tools, sha256=self.xctest.tools.sha256), sanitation, egress)
        self.config.validate()
        self.runs = RunStore(self.root / "runs",
                             environment_digest=contracts.digest("ios-service-environment"),
                             disk_limit=8 * 1024 ** 3)
        self.operations = IOSMobileOperationStore(self.runs, self.config.definition,
                                                  self.root / "operations")
        self._cleanups.append(self.operations.close)
        self.candidate = BlobSet((("candidate.ipa", candidate_ipa_bytes),))
        self.context = MobileContext(
            "ios-service-" + contracts.digest(str(self.root))[:20],
            contracts.digest("ios-service-request"), "b" * 64,
            self.config.definition.project_digest, self.config.application_id, "1" * 64,
            candidate_archive_digest, self.config.scope_digest,
            self.config.runtime_policy_digest, "ios-service-nonce")
        build_id = "candidate_" + contracts.digest({
            "operation": self.context.operation_id,
            "request": self.context.request_digest,
        })[:32]
        build = {"id": build_id, "applicationId": self.context.application_id,
                 "revision": build_id, "sourceDigest": self.context.source_digest,
                 "artifactDigest": self.context.artifact_digest,
                 "provenance": "trusted-build"}
        approval = contracts.issue_substitution_approval(
            qualification_digest=self.approved.qualification_digest,
            recording_digest=self.approved.recording_digest,
            specification_digest=self.approved.specification_digest,
            candidate_build_id=build_id,
            candidate_build_digest=contracts.digest(build))
        self.execution = self.env.registry.authorize_candidate_build(
            self.approved, build, approval)
        self.stage_path = self.root / "sanitation-stage"
        self.mode_path = self.root / "sanitation-mode"
        self.cancel_event = None
        self.stage_path.write_text("launch")
        self.mode_path.write_text("valid")
        self.doubles = []
        self._patches = []
        self._install_http_double_factory()

    def _approve(self, original):
        spec = coordinate_specification(original)
        qualification = g4_support.qualification(self.env.project, original, spec, self.plan)
        # Keep the fixture failure local and actionable while its contract is
        # being assembled; ScenarioRegistry intentionally returns one generic
        # qualification error.
        contracts.validate_qualification_bindings(
            qualification, self.env.project, original["original"], spec)
        return self.env.registry.register(
            self.env.registration, original, spec, qualification,
            g4_support.runtime_policy(), fixture_plans=(self.plan,))

    def _tool_extra(self):
        run_file = self.root / "runtime-launch.json"
        self.runtime_file = run_file
        stage_file = self.root / "sanitation-stage"
        mode_file = self.root / "sanitation-mode"
        install_file = self.root / "installed-build"
        policy = validate_ios_sanitation_policy(sanitation_document())
        return f"""
if args[:3] == ['device','copy','from']:
    destination=pathlib.Path(args[args.index('--destination')+1])
    output=pathlib.Path(args[args.index('--json-output')+1])
    runtime=json.loads(pathlib.Path({str(run_file)!r}).read_bytes())
    stage=pathlib.Path({str(stage_file)!r}).read_text()
    mode=pathlib.Path({str(mode_file)!r}).read_text()
    if mode == 'malformed':
        identity={{'schemaVersion':2,'kind':'ios-runtime-identity','bundleId':runtime['bundleId'],'buildId':runtime['buildId'],'profileDigest':runtime['profileDigest'],'runId':runtime['runId']}}
    else:
        receipt={{'schemaVersion':1,'kind':'ios-app-sanitation-receipt','policyDigest':runtime['sanitationPolicyDigest'],'runId':runtime['runId'],'stage':stage,'startedAtMs':1,'completedAtMs':2,'pathCount':1,'userDefaultsKeyCount':1,'keychainItemCount':1,'status':'complete'}}
        identity={{'schemaVersion':2,'kind':'ios-runtime-identity','bundleId':runtime['bundleId'],'buildId':runtime['buildId'],'profileDigest':runtime['profileDigest'],'runId':runtime['runId'],'startedAtMs':1,'sanitation':receipt}}
    destination.write_text(json.dumps(identity))
    result={{}}
    destination=output
elif args[:3] == ['device','install','app']:
    role='candidate-28' if '/candidate/' in args[5] else 'original-27'
    pathlib.Path({str(install_file)!r}).write_text(role)
    result={{'installedApplications':[{{'bundleID':BUNDLE}}]}}
elif args[:3] == ['device','info','apps']:
    version=pathlib.Path({str(install_file)!r}).read_text() if pathlib.Path({str(install_file)!r}).exists() else '27'
    result={{'apps':[{{'bundleIdentifier':BUNDLE,'version':'1.0','bundleVersion':version}}]}}
"""

    def _install_http_double_factory(self):
        original = helper_fixtures.OwnedHTTPDouble
        fixture = self
        class Factory:
            def __call__(self, address, port, token):
                launch = None
                # Launches are recorded by the runner patch below; token is the
                # immutable key emitted in the actual XCTest payload.
                for candidate in fixture._launches:
                    if candidate._token == token:
                        launch = candidate
                        break
                if launch is None:
                    raise AssertionError("HTTP double was requested before issued XCTest launch")
                double = SanitationHTTPDouble(address, port, token,
                    stage_path=fixture.stage_path, mode_path=fixture.mode_path,
                    cancel_event=fixture.cancel_event)
                # egress 정책이 묶인 fixture는 카운터 증거 방출 모드를 노출한다.
                double.network_evidence_mode = getattr(
                    fixture, "network_evidence_mode", "emit")
                double.egress_delta = getattr(fixture, "egress_delta", 0)
                double.launch = launch
                double.release = fixture.xctest.release
                double.frame.update(width=400, height=800, logicalWidth=400,
                                    logicalHeight=800)
                now = int(time.monotonic() * 1000) - 100
                double.frame["nativeTiming"] = {"version": 1,
                    "nativeClockId": "ios-mach-continuous",
                    "nativeIncarnation": "native_helper_double",
                    "captureStartMs": now, "captureEndMs": now}
                fixture.doubles.append(double)
                return double
        self._launches = []
        from reproof.ios_mobile_xctest import IOSXCTestRunner
        original_prepare = IOSXCTestRunner.prepare
        def prepare(runner, *args, **kwargs):
            if fixture.xctest.release.exists():
                fixture.xctest.release.unlink()
            fixture.stage_path.write_text("launch")
            launch = original_prepare(runner, *args, **kwargs)
            fixture._launches.append(launch)
            if "runtimeIdentity" in launch.payload:
                fixture.runtime_file.write_text(json.dumps(launch.payload["runtimeIdentity"]))
            return launch
        self._patches.append(unittest.mock.patch.object(IOSXCTestRunner, "prepare", prepare))
        self._patches.append(unittest.mock.patch("reproof.ios_mobile_helper.TunnelClient", new=Factory()))
        self._patches.append(unittest.mock.patch(
            "reproof.live.native_frame_clock.NativeFrameClock.frame_arguments",
            return_value={"timing_source": "native-unmapped"}))
        for patcher in self._patches:
            patcher.start()
        self._cleanups.extend(patcher.stop for patcher in reversed(self._patches))

    def _bounds(self, *, cancellation=None, seconds=60):
        return {"cancellation": cancellation or threading.Event(),
                "deadline_monotonic": time.monotonic() + seconds}

    def admit(self):
        return self.operations.admit(self.context, self.candidate, self.config.read_baselines())

    def close(self):
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                cleanup()
            except Exception:
                pass
        self.temp.cleanup()


__all__ = ["IOSServiceFixture", "coordinate_specification", "sanitation_document",
           "SanitationHTTPDouble"]
