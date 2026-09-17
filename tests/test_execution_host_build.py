"""Host-build execution class: pinned toolchain, no isolation claim."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from reproloop.execution.artifacts import ArtifactValidationAuthority, BlobSet
from reproloop.execution.backend import ExecutionDenied, QualificationAuthority
from reproloop.execution.journal import RunStore
from reproloop.execution.qualification import qualify_host_build
from reproloop.execution.resources import HostBuildBundle, provision_host, ResourceError
from reproloop.execution.runtime import HostBuildBackend, run_host_recipe
from reproloop.repair_composition import ProtectedRepairComposition
from reproloop.repair_execution import ProtectedBuildSupervisor, RepairExecutionError


def _sha256(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _make_tools(root):
    """테스트용 호스트 도구: 시스템 sleep과 출력을 쓰는 빌드 스크립트.

    스크립트 내용에 nonce를 넣어 machine scope digest가 매 실행 달라지게 한다 —
    lease 마커가 이전 실행의 canonical root에 묶여 있으면 재실행이 거절된다.
    """
    import uuid
    builder = root / "build-tool.sh"
    builder.write_text("#!/bin/sh\n# nonce " + uuid.uuid4().hex
                       + "\nset -e\ncp ../input/source.bin candidate.out\n")
    builder.chmod(0o500)
    return [
        {"id": "probe-sleep", "version": "system", "path": "/bin/sleep",
         "artifactDigest": _sha256("/bin/sleep"),
         "sizeBytes": os.lstat("/bin/sleep").st_size},
        # 스크립트 도구의 shebang 인터프리터도 고정 도구로 선언돼야 verify를 통과한다.
        {"id": "shell", "version": "system", "path": "/bin/sh",
         "artifactDigest": _sha256("/bin/sh"),
         "sizeBytes": os.lstat("/bin/sh").st_size},
        {"id": "builder", "version": "1.0", "path": str(builder),
         "artifactDigest": _sha256(builder), "sizeBytes": builder.stat().st_size},
    ]


def _tool_path(tools, tool_id):
    return next(tool["path"] for tool in tools if tool["id"] == tool_id)


def _metadata(tools, builder_path):
    import uuid
    # env id는 실행 scope(lease·격리 표시)의 이름이다 — 매 실행 고유해야
    # 이전 canonical root에 묶인 lease 마커와 충돌하지 않는다.
    env_id = "host-build-env-" + uuid.uuid4().hex[:12]
    return {
        "schemaVersion": 1,
        "environment": {
            "schemaVersion": 1, "id": env_id, "executionClass": "host-build",
            "architecture": "arm64", "network": "unrestricted", "transport": "host-process",
            "controls": ["toolchain-pinned", "process-termination", "cleanup"],
            "resources": {"cpuCount": 2, "memoryMiB": 1024,
                          "diskBytes": 16 * 1024 ** 2, "timeoutMs": 60_000},
        },
        "tools": tools,
        "catalog": [
            {"id": "host-qualification-probe", "executionClass": "host-build",
             "argv": ["/bin/sleep", "30"], "artifactPolicyId": "probe-artifacts",
             "cleanupPolicyId": "dispose-run", "outputPaths": ["probe.out"],
             "maxOutputBytes": 4096, "timeoutMs": 60_000},
            {"id": "host-sample-build", "executionClass": "host-build",
             "argv": [str(builder_path)], "artifactPolicyId": "unsigned-ios-ipa",
             "cleanupPolicyId": "dispose-run", "outputPaths": ["candidate.out"],
             "maxOutputBytes": 4096, "timeoutMs": 60_000},
        ],
    }


class HostBuildFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        root.chmod(0o700)
        self.root = root
        self.tools = _make_tools(root)
        builder_path = _tool_path(self.tools, "builder")
        self.bundle = provision_host(root / "host-bundle", metadata=_metadata(self.tools, builder_path))
        self.store = RunStore(root / "host-state", environment_digest=self.bundle.environment_digest,
                              disk_limit=64 * 1024 ** 2)
        self.authority = QualificationAuthority()

    def tearDown(self):
        self.tmp.cleanup()

    def _tamper_builder(self):
        path = Path(_tool_path(self.tools, "builder"))
        path.chmod(0o700)
        path.write_text("#!/bin/sh\nexit 1\n")

    def _plan(self):
        return {"schemaVersion": 1, "id": "host-validation", "projectDigest": "a" * 64,
                "candidateReports": "supplemental-only",
                "checks": [{"id": "independent-ui", "recipeId": "regression-ui",
                            "kind": "external-observation", "evidenceSourceId": "observer"}]}

    def _route(self, execution_class="host-build"):
        return {"schemaVersion": 1, "id": "host-build-route", "projectDigest": "a" * 64,
                "backendId": "host-builder", "executionClass": execution_class,
                "environmentDigest": self.bundle.environment_digest, "inputKind": "sealed-source",
                "recipeId": "host-sample-build", "artifactPolicyId": "unsigned-ios-ipa",
                "validationPlanId": "host-validation", "cleanupPolicyId": "dispose-run"}

    def _qualify(self):
        return qualify_host_build("host-builder", self.authority, self.bundle, self.store)

    def _artifact_authority(self):
        authority = ArtifactValidationAuthority()
        authority.register("unsigned-ios-ipa", paths=("candidate.out",), max_bytes=4096,
                           checker=lambda blobs: True)
        return authority

    def _backend(self):
        outcome = self._qualify()
        assert outcome.qualification is not None, outcome.report
        return HostBuildBackend("host-builder", self.authority, self.bundle, self.store), outcome

    def _supervisor(self, route=None):
        backend, outcome = self._backend()
        plan = self.authority.register_validation_plan(self._plan())
        trusted_route = self.authority.register_execution_route(route or self._route())
        return ProtectedBuildSupervisor(backend, qualification=outcome.qualification,
            route=trusted_route, validation_plan=plan, artifact_authority=self._artifact_authority(),
            application_id="ios-app"), backend


class HostBundleTests(HostBuildFixture):
    def test_provision_and_load_round_trip(self):
        loaded = HostBuildBundle.load(self.root / "host-bundle")
        self.assertEqual(loaded.environment_digest, self.bundle.environment_digest)
        self.assertEqual(loaded.metadata["environment"]["executionClass"], "host-build")
        self.assertEqual(loaded.recipe("host-sample-build")["argv"][0],
                         _tool_path(self.tools, "builder"))

    def test_tampered_tool_is_rejected(self):
        self._tamper_builder()
        with self.assertRaises(ResourceError):
            HostBuildBundle.load(self.root / "host-bundle")

    def test_missing_probe_recipe_is_rejected(self):
        metadata = _metadata(self.tools, _tool_path(self.tools, "builder"))
        metadata["catalog"] = [metadata["catalog"][1]]
        with self.assertRaises(ResourceError):
            provision_host(self.root / "bad-bundle", metadata=metadata)

    def test_recipe_must_run_declared_tool(self):
        metadata = _metadata(self.tools, _tool_path(self.tools, "builder"))
        # 번들에 선언되지 않은 실행 파일은 거절된다.
        metadata["catalog"][1]["argv"] = ["/usr/bin/true"]
        with self.assertRaises(ResourceError):
            provision_host(self.root / "bad-bundle-2", metadata=metadata)

    def test_script_interpreter_must_be_pinned(self):
        # shebang이 가리키는 인터프리터가 고정 도구 목록에 없으면 거절한다.
        tools = [tool for tool in self.tools if tool["id"] != "shell"]
        metadata = _metadata(tools, _tool_path(tools, "builder"))
        with self.assertRaises(ResourceError):
            provision_host(self.root / "bad-bundle-3", metadata=metadata)

    def test_public_dir_is_rejected(self):
        (self.root / "host-bundle").chmod(0o755)
        with self.assertRaises(ResourceError):
            HostBuildBundle.load(self.root / "host-bundle")


class HostQualificationTests(HostBuildFixture):
    def test_full_measurement_issues_host_build_qualification(self):
        outcome = self._qualify()
        self.assertTrue(outcome.report["qualified"])
        self.assertEqual(outcome.report["executionClass"], "host-build")
        self.assertEqual(outcome.report["isolation"], "host")
        self.assertFalse(outcome.report["actualVM"])
        self.assertEqual([item["probeId"] for item in outcome.report["probes"]],
                         ["toolchain-boundary", "process-termination", "cleanup"])
        accepted = self.authority.require_qualification(outcome.qualification, backend_id="host-builder",
            execution_class="host-build", environment_digest=self.bundle.environment_digest,
            evaluated_at_ms=int(time.time() * 1000))
        self.assertIs(accepted, outcome.qualification)

    def test_new_measurement_revokes_previous_qualification(self):
        first = self._qualify().qualification
        second = self._qualify().qualification
        self.assertIsNot(first, second)
        with self.assertRaises(ExecutionDenied):
            self.authority.require_qualification(first, backend_id="host-builder",
                execution_class="host-build", environment_digest=self.bundle.environment_digest,
                evaluated_at_ms=int(time.time() * 1000))

    def test_tampered_tool_blocks_qualification(self):
        self._tamper_builder()
        outcome = self._qualify()
        self.assertIsNone(outcome.qualification)
        # 변조된 도구는 verify 단계에서 차단돼 probe가 아예 시작되지 않는다.
        self.assertFalse(any(item["passed"] for item in outcome.report["probes"]))

    def test_rejected_for_guest_bundle_and_foreign_authority(self):
        with self.assertRaises(ExecutionDenied):
            qualify_host_build("host-builder", object(), self.bundle, self.store)
        with self.assertRaises(ExecutionDenied):
            qualify_host_build("host-builder", self.authority, object(), self.store)


class HostBackendTests(HostBuildFixture):
    def test_execute_runs_pinned_recipe_and_returns_host_label(self):
        supervisor, backend = self._supervisor()
        sources = BlobSet((("source.bin", b"candidate bytes"),))
        proof = supervisor.build(sources, operation_id="host_build_1",
            repair_plan_digest="b" * 64, cancellation=threading.Event())
        self.assertEqual(proof.public()["buildIsolation"], "host")
        self.assertEqual(proof.validated_artifacts.blobs.entries, (("candidate.out", b"candidate bytes"),))
        state = backend.store.status("host_build_1")
        self.assertEqual(state["state"], "succeeded")
        self.assertFalse((self.root / "host-state" / "runs" / "host_build_1").exists())

    def test_guest_route_cannot_execute_on_host_backend(self):
        supervisor, _ = self._supervisor(route=self._route(execution_class="build-guest"))
        sources = BlobSet((("source.bin", b"candidate bytes"),))
        with self.assertRaises(RepairExecutionError):
            supervisor.build(sources, operation_id="host_build_2",
                repair_plan_digest="b" * 64, cancellation=threading.Event())

    def test_composition_qualify_build_dispatches_host(self):
        owner = ProtectedRepairComposition(authority=self.authority)
        supervisor = owner.qualify_build(bundle=self.bundle, store=self.store, route=self._route(),
            validation_plan=self._plan(), artifact_authority=self._artifact_authority(),
            application_id="ios-app")
        self.assertIs(type(supervisor.backend), HostBuildBackend)
        self.assertEqual(supervisor.backend.isolation, "host")

    def test_host_bundle_rejected_for_guest_route_in_composition(self):
        owner = ProtectedRepairComposition(authority=self.authority)
        with self.assertRaises(RepairExecutionError):
            owner.qualify_build(bundle=self.bundle, store=self.store,
                route=self._route(execution_class="build-guest"), validation_plan=self._plan(),
                artifact_authority=self._artifact_authority(), application_id="ios-app")


class HostHardeningTests(HostBuildFixture):
    """Codex 리뷰로 확인된 적대적 시나리오: 후보가 run 디렉터리·저널을 공격한다."""

    def test_reconcile_is_always_rejected_for_host(self):
        backend, _ = self._backend()
        with self.assertRaises(ExecutionDenied):
            backend.reconcile("host_build_x", "f" * 64)

    def test_cleanup_does_not_follow_replaced_run_root(self):
        from reproloop.execution.runtime import _discard_run_tree
        # 후보가 run 디렉터리를 외부 경로의 symlink로 바꿔친 상황을 재현한다.
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "victim.txt").write_text("keep me")
        swapped = self.root / "run-swapped"
        os.symlink(outside, swapped)
        self.assertFalse(_discard_run_tree(swapped))
        self.assertTrue((outside / "victim.txt").exists())

    def test_cleanup_unlinks_nested_symlink_without_touching_target(self):
        from reproloop.execution.runtime import _discard_run_tree
        outside = self.root / "outside2"
        outside.mkdir()
        (outside / "victim.txt").write_text("keep me")
        run_dir = self.root / "run-nested"
        run_dir.mkdir()
        os.symlink(outside / "victim.txt", run_dir / "link")
        (run_dir / "real.txt").write_text("run file")
        self.assertTrue(_discard_run_tree(run_dir))
        self.assertTrue((outside / "victim.txt").exists())
        self.assertFalse(any(run_dir.iterdir()))

    def test_candidate_created_files_are_cleaned(self):
        supervisor, backend = self._supervisor()
        sources = BlobSet((("source.bin", b"candidate bytes"),))
        proof = supervisor.build(sources, operation_id="host_build_extra",
            repair_plan_digest="b" * 64, cancellation=threading.Event())
        self.assertEqual(proof.public()["buildIsolation"], "host")
        # 빌드가 만든 부산물을 포함해 run 디렉터리 전체가 비워졌어야 한다.
        self.assertFalse((self.root / "host-state" / "runs" / "host_build_extra").exists())

    def test_cancelled_before_launch_starts_no_process(self):
        supervisor, _ = self._supervisor()
        cancel = threading.Event()
        cancel.set()  # launch 전에 이미 취소됨 — 프로세스가 시작되면 안 된다.
        sources = BlobSet((("source.bin", b"candidate bytes"),))
        with self.assertRaises(RepairExecutionError):
            supervisor.build(sources, operation_id="host_build_cancel",
                repair_plan_digest="b" * 64, cancellation=cancel)
        self.assertFalse((self.root / "host-state" / "runs" / "host_build_cancel").exists())

    def test_prelaunch_cancel_via_runtime_path(self):
        # 저널 취소 레코드가 있으면 runtime은 프로세스를 시작하지 않는다 —
        # supervisor 사전검사가 아니라 실제 launch 경로를 검증한다.
        self._backend()
        with self.store.machine_lease(self.bundle.machine_digest, kind='host'):
            with self.store.admit("op_pre_cancel", "e" * 64,
                                  disk_bytes=self.bundle.overlay_bytes) as run:
                self.store.cancel("op_pre_cancel", "e" * 64)
                recipe = self.bundle.recipe("host-sample-build")
                outcome = run_host_recipe(self.bundle, recipe,
                    BlobSet((("source.bin", b"x"),)), run, time.monotonic() + 30)
                self.assertFalse(outcome["lifecycle"]["started"])
                self.assertTrue(outcome["stopped"])
                run.finish("failed", stopped=outcome["stopped"])
        self.assertEqual(self.store.status("op_pre_cancel")["state"], "cancelled")


class HostConfigurationTests(unittest.TestCase):
    """공개 설정 수준의 opt-in: hostPath는 host-build route에만 허용된다."""

    def _profile(self, build_key="hostPath", build_class="host-build"):
        journal = lambda root, env="c" * 64: {"root": root, "environmentDigest": env,
                                "diskBudgetBytes": 16 * 1024 ** 2}
        signing_policy = {"schemaVersion": 1, "id": "ios-policy", "platform": "ios",
                          "applicationId": "ios-app", "identityReferenceId": "ios-key",
                          "entitlementsDigest": "1" * 64, "provisioningReferenceId": "ios-prof",
                          "tool": "host-codesign-fixed", "candidateHooks": "forbidden",
                          "artifactRelation": "pre-post-digests"}
        build_route = {"schemaVersion": 1, "id": "host-route", "projectDigest": "a" * 64,
                       "backendId": "host-builder", "executionClass": build_class,
                       "environmentDigest": "c" * 64, "inputKind": "sealed-source",
                       "recipeId": "host-sample-build", "artifactPolicyId": "unsigned-ios-ipa",
                       "validationPlanId": "host-validation", "cleanupPolicyId": "dispose-run"}
        mobile_route = {"schemaVersion": 1, "id": "ios-route", "projectDigest": "a" * 64,
                        "backendId": "ios-device", "executionClass": "mobile-device",
                        "environmentDigest": "2" * 64, "inputKind": "validated-artifact",
                        "recipeId": "ios-replay", "artifactPolicyId": "signed-ios-ipa",
                        "validationPlanId": "host-validation", "cleanupPolicyId": "ios-cleanup",
                        "platform": "ios", "applicationId": "ios-app", "signingPolicyId": "ios-policy"}
        plan = {"schemaVersion": 1, "id": "host-validation", "projectDigest": "a" * 64,
                "candidateReports": "supplemental-only",
                "checks": [{"id": "check", "recipeId": "ios-replay",
                            "kind": "external-observation", "evidenceSourceId": "observer"}]}
        return {"id": "profile-1", "projectId": "project-1", "projectDigest": "a" * 64,
                "applicationId": "ios-app", "originalBuildId": "original-1",
                "platform": "ios", "deviceId": "device-1", "runtimePolicyDigest": "b" * 64,
                "build": {build_key: "/opt/host-bundle", "route": build_route,
                          "journal": journal("/var/mobile-journal/build")},
                "signing": {"toolsPath": "/opt/signing-tools", "toolsManifestSha256": "d" * 64,
                            "definition": {"path": "/opt/signing-def.json", "sha256": "e" * 64},
                            "policy": signing_policy, "journal": journal("/var/mobile-journal/sign"),
                            "ownerRoot": "/var/mobile-owner/sign"},
                "mobile": {"definition": {"path": "/opt/mobile-def.json", "sha256": "f" * 64},
                           "route": mobile_route,
                           "journal": journal("/var/mobile-journal/mobile", env="2" * 64),
                           "ownerRoot": "/var/mobile-owner/mobile"},
                "validation": {"plan": plan,
                               "observers": {"path": "/opt/observers.json", "sha256": "0" * 64}}}

    def _document(self, **kwargs):
        return {"schemaVersion": 1, "kind": "reproloop-protected-service",
                "profiles": [self._profile(**kwargs)]}

    def test_host_build_profile_with_host_path_is_accepted(self):
        from reproloop.repair_configuration import ProtectedServiceConfiguration
        configuration = ProtectedServiceConfiguration(self._document())
        self.assertEqual(configuration.document["profiles"][0]["build"]["route"]["executionClass"],
                         "host-build")

    def test_host_path_is_rejected_for_guest_route(self):
        from reproloop.repair_configuration import (ProtectedServiceConfiguration,
            ProtectedServiceConfigurationError)
        with self.assertRaises(ProtectedServiceConfigurationError):
            ProtectedServiceConfiguration(self._document(build_class="build-guest"))

    def test_bundle_path_is_rejected_for_host_route(self):
        from reproloop.repair_configuration import (ProtectedServiceConfiguration,
            ProtectedServiceConfigurationError)
        with self.assertRaises(ProtectedServiceConfigurationError):
            ProtectedServiceConfiguration(self._document(build_key="bundlePath"))


if __name__ == "__main__":
    unittest.main()
