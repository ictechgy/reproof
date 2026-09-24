"""Thread and abandonment boundaries for the iOS G4 owner bridge."""
import threading
import time
import unittest
import json
import hashlib
import io
import plistlib
import zipfile
import copy
from dataclasses import replace
from unittest.mock import patch

from reproof import contracts
from reproof.execution.artifacts import BlobSet
from reproof.ios_mobile_g4 import (
    IOSG4Error, IOSG4Provider, IOSOwnerCommandPump, _RequestKind,
)
from reproof.ios_mobile_helper import IOSHelperChannel
from reproof.ios_device_tools import IOSDeviceToolError
from reproof.ios_mobile_identity import IOSInstalledIdentityObservation
from reproof.ios_mobile_inputs import IOSBaselineReference
from reproof.ios_profile import validate_ios_profile
from reproof.live.authority import ProviderResult
from tests import test_ios_mobile_helper as helper_fixture
from tests import test_ios_mobile_runtime_identity as runtime_fixture
from tests.g4_support import G4Environment
from tests.test_worker_profiles import physical_ios_document


class _QueueOwner:
    def __init__(self):
        self.pump = IOSOwnerCommandPump(self, max_pending=4)
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def _execute_owner_request(self, request):
        self.calls.append((request.kind, threading.get_ident(), request.action))
        self.entered.set()
        if request.action == "block":
            self.release.wait(2)
        if request.abandoned:
            raise IOSG4Error("ios_g4_unknown", "request abandoned", 409, unknown=True)
        return {"ok": True, "ownerThread": threading.get_ident()}

    def _poll_owner(self, _deadline):
        return None


def _submit(owner, action, *, deadline=None, cancellation=None):
    cancellation = cancellation or threading.Event()
    deadline = deadline or time.monotonic() + 2
    return owner.pump.submit(
        _RequestKind.EXECUTE, cancellation=cancellation,
        deadline_monotonic=deadline, permit=object(), action=action, payload={})


class IOSMobileG4PumpTests(unittest.TestCase):
    def pump_until(self, owner, predicate, deadline=None):
        deadline = deadline or time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            owner.pump.pump_once(deadline_monotonic=deadline)
            time.sleep(.001)
        self.assertTrue(predicate())

    def test_three_service_callers_execute_on_one_owner_thread(self):
        owner = _QueueOwner()
        results, errors = [], []

        def caller(index):
            try:
                results.append((index, _submit(owner, "call-" + str(index))))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=caller, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        self.pump_until(owner, lambda: len(results) + len(errors) == 3)
        for thread in threads:
            thread.join(1)
        self.assertFalse(errors)
        self.assertEqual(len(results), 3)
        self.assertEqual({thread_id for _, thread_id, _ in owner.calls},
                         {owner.pump.owner_thread_id})

    def test_abandoned_queued_request_never_dispatches(self):
        owner = _QueueOwner()
        first_result, second_error = [], []
        first = threading.Thread(target=lambda: self._capture(
            first_result, lambda: _submit(owner, "block")))
        first.start()
        pump_thread = threading.Thread(target=self._pump_until_settled, args=(owner,))
        pump_thread.start()
        self.assertTrue(owner.entered.wait(1))
        second = threading.Thread(target=lambda: self._capture(
            second_error, lambda: _submit(owner, "abandoned", deadline=time.monotonic() + .05)))
        second.start()
        second.join(1)
        self.assertFalse(second.is_alive())
        owner.release.set()
        pump_thread.join(2)
        first.join(1)
        self.assertEqual([action for _, _, action in owner.calls], ["block"])
        self.assertEqual(len(second_error), 1)

    def test_abandoned_inflight_request_cannot_publish_success(self):
        owner = _QueueOwner()
        result, errors = [], []
        caller = threading.Thread(target=lambda: self._capture(
            errors, lambda: result.append(_submit(
                owner, "block", deadline=time.monotonic() + .05))))
        caller.start()
        pump_thread = threading.Thread(target=self._pump_until_settled, args=(owner,))
        pump_thread.start()
        self.assertTrue(owner.entered.wait(1))
        caller.join(1)
        self.assertFalse(caller.is_alive())
        owner.release.set()
        pump_thread.join(2)
        self.assertEqual(result, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(owner.calls), 1)

    def test_owner_thread_identity_is_fenced(self):
        owner = _QueueOwner()
        owner.pump.pump_once(deadline_monotonic=time.monotonic() + 1)
        errors = []
        thread = threading.Thread(target=lambda: self._capture(
            errors, owner.pump.pump_once, time.monotonic() + 1))
        thread.start();thread.join(1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(getattr(errors[0], "code", None), "ios_g4_owner_thread")

    @staticmethod
    def _capture(errors, function, deadline=None):
        try:
            if deadline is None:
                function()
            else:
                function(deadline_monotonic=deadline)
        except Exception as error:
            errors.append(error)

    @staticmethod
    def _pump_until_settled(owner):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            owner.pump.pump_once(deadline_monotonic=deadline)
            if owner.pump.pending == 0 and owner.release.is_set():
                return
            time.sleep(.001)


class IOSMobileG4ProviderContractTests(unittest.TestCase):
    def test_real_owner_start_publishes_first_frame_and_uses_distinct_runtime_profile(self):
        runtime = runtime_fixture.RealIOSRuntimeIdentityTests(methodName="runTest")
        runtime.setUp()
        environment = G4Environment()
        self.addCleanup(environment.close)
        self.addCleanup(runtime.doCleanups)
        case = runtime.case
        entries, infos = {}, {}
        with zipfile.ZipFile(io.BytesIO(case.g.c.body)) as archive:
            for entry in archive.infolist():
                entries[entry.filename] = archive.read(entry)
                infos[entry.filename] = entry
        info_name = next(name for name in entries
                         if name.endswith("/Info.plist")
                         and "/Frameworks/" not in name and "/PlugIns/" not in name)
        info = plistlib.loads(entries[info_name])
        info["CFBundlePackageType"] = "APPL"
        entries[info_name] = plistlib.dumps(info)
        rewritten = io.BytesIO()
        with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in entries.items():
                archive.writestr(infos[name], body)
        case.g.c.body = rewritten.getvalue()
        case.g.c.artifacts = BlobSet((("candidate.ipa", case.g.c.body),))
        case.g.c.context = replace(
            case.g.c.context,
            artifact_digest=hashlib.sha256(case.g.c.body).hexdigest())
        with case.owned() as (owner, runner):
            probe = case.prepare(runner)
            intent, _state = owner.operations._records(
                owner.operation.context.operation_id, owner._directory)
            archive = intent["roles"]["candidate"]
            archive_identity = IOSBaselineReference(
                "candidate", case.g.c.selected.bundle_id,
                owner.operation.archive_path("candidate"), archive["sha256"],
                archive["bytes"]).read()[1]
            profile_document = physical_ios_document(archive_identity["treeDigest"])
            profile_document["applicationId"] = case.g.c.selected.application_id
            profile_document["projectDigest"] = owner.operations.definition.project_digest
            profile_document["bundle"] = case.g.c.selected.bundle_id
            profile_document["launchTarget"] = {
                "kind": "bundle", "value": profile_document["bundle"]}
            profile_document["artifact"].update(
                bytes=archive_identity["bytes"],
                bundleVersion=archive_identity["bundleVersion"],
                bundleBuild=archive_identity["bundleBuild"])
            profile = validate_ios_profile(profile_document)
            launch = runner.prepare(
                role="candidate", iteration=2,
                application_id=case.g.c.selected.application_id,
                profile_digest=profile.digest,
                actions=tuple(profile.data["capabilities"]["actions"]),
                cancellation=threading.Event(),
                deadline_monotonic=time.monotonic() + 10)
            runtime.run_file.write_text(json.dumps(launch.payload["runtimeIdentity"]))
            identity = IOSInstalledIdentityObservation(
                "install-candidate", owner.binding_digest,
                owner.operation.context.digest, "candidate",
                launch.payload["appDigests"]["candidate"], profile.bundle,
                archive_identity["bundleVersion"], archive_identity["bundleBuild"],
                "a" * 64)
            owner._identity_results["install-candidate"] = identity
            native_double = helper_fixture.OwnedHTTPDouble(
                launch._endpoint, runner.tools.port, launch._token)
            native_double.launch = launch
            native_double.release = case.release
            native_call = native_double.call
            def controlled_call(path, body=None, timeout=5, binary=False):
                if path.startswith("/frames/after/") and int(path.rsplit("/", 1)[1]) >= 1:
                    error = IOSDeviceToolError("frame_unavailable")
                    raise error
                return native_call(path, body, timeout, binary)
            native_double.call = controlled_call
            session = environment.lab.create_session("device", "owner", "g4")
            # The synthetic Lab has no native authority of its own; use the
            # issued owner authority only for the clock observation boundary.
            environment.lab.authority = owner.device._authority
            wrong_profile_document = copy.deepcopy(profile_document)
            wrong_profile_document["projectDigest"] = "0" * 64
            wrong_profile = validate_ios_profile(wrong_profile_document)
            with self.assertRaises(IOSG4Error):
                IOSG4Provider(runner, launch, identity, wrong_profile, owner)
            provider = IOSG4Provider(runner, launch, identity, profile, owner)
            provider.bind_authority(owner.device, launch.payload["providerIncarnation"])
            cancellation = threading.Event()
            deadline = time.monotonic() + 20
            provider.set_request_context(cancellation, deadline)
            result, errors = [], []

            def start():
                try:
                    result.append(provider.start_authorized(session, environment.lab,
                                                            permit))
                except Exception as error:
                    errors.append(error)

            with patch("reproof.ios_mobile_helper.TunnelClient",
                       return_value=native_double):
                permit = case.permit(launch)
                stale = replace(permit, provider_incarnation="ios-xctest-stale")
                with self.assertRaises(IOSG4Error):
                    provider.start_authorized(session, environment.lab, stale)
                self.assertEqual(native_double.calls, [])
                worker = threading.Thread(target=start)
                worker.start()
                try:
                    while worker.is_alive():
                        provider.pump.pump_once(deadline_monotonic=deadline)
                except Exception:
                    raise
                worker.join(1)
            self.assertFalse(errors)
            self.assertEqual(len(result), 1)
            self.assertEqual(provider.owner_thread_id, threading.get_ident())
            frame = environment.lab.frame(session["id"], "owner")
            self.assertEqual(frame["imageBase64"], native_double.frame["imageBase64"])
            self.assertNotEqual(identity.source_app_digest, profile.data["artifact"]["sha256"])
            self.assertNotEqual(profile.digest, launch.payload["runtimeIdentity"]["profileDigest"])

            case.release.write_text("finish G4 owner start")
            startup_result_digest = contracts.digest({"kind": "startup", "status": "succeeded"})
            owner.device.confirm_operation(
                permit, ProviderResult("receipt-g4-start", "succeeded", startup_result_digest))
            cleanup_payload = IOSHelperChannel.command_payload("cleanup", {})
            admission = owner.device.admit_operation(
                operation_id="g4-cleanup", payload_digest=contracts.digest(cleanup_payload),
                session_id="g4-owner-session", sequence=2)
            cleanup_permit = owner.device.prepare_dispatch(
                admission, provider_incarnation=launch.payload["providerIncarnation"])
            cleanup_result, cleanup_errors = [], []

            def close():
                try:
                    cleanup_result.append(provider.close_authorized(cleanup_permit))
                except Exception as error:
                    cleanup_errors.append(error)

            with patch("reproof.ios_mobile_helper.TunnelClient",
                       return_value=native_double):
                closing = threading.Thread(target=close)
                closing.start()
                while closing.is_alive():
                    provider.pump.pump_once(deadline_monotonic=deadline)
                closing.join(1)
            self.assertFalse(cleanup_errors)
            self.assertTrue(cleanup_result[0]["ok"])
            self.assertTrue(cleanup_result[0]["target"]["terminationConfirmed"])
            self.assertEqual(provider.close_authorized(cleanup_permit), cleanup_result[0])
            with self.assertRaises(IOSG4Error):
                provider.close_authorized(replace(cleanup_permit))
            environment.lab.close_session(session["id"], "owner")
            self.assertTrue(runner.close())

    def test_locator_and_semantic_observation_are_explicitly_unsupported(self):
        provider = object.__new__(IOSG4Provider)
        with self.assertRaises(IOSG4Error) as locator:
            provider.resolve_locator({}, "target")
        with self.assertRaises(IOSG4Error) as observation:
            provider.observe()
        self.assertEqual(locator.exception.code, "unsupported_operation")
        self.assertEqual(observation.exception.code, "unsupported_operation")

    def test_unconfirmed_target_cleanup_stays_unknown(self):
        result = IOSG4Provider._bounded_cleanup_result({
            "ok": True,
            "host": {"terminated": True, "terminationConfirmed": True},
            "helper": {"terminationConfirmed": True},
            "target": {"terminationConfirmed": False},
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "cleanup_uncertain")


if __name__ == "__main__":
    unittest.main()
