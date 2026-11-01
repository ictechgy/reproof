"""Simulator 빌드 argv, .app 탐색, 빌드 파이프라인(재사용·끊긴 빌드 폐기) 테스트. Xcode 없이 FakeToolRunner로 실행된다."""
import dataclasses
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from reproof.simple.build import PRODUCTS_SUBPATH, find_app_bundle, simulator_build_argv
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.pipeline import default_workspace_root, prepare_build
from tests.simple_support import FakeToolRunner, sample_config

BUNDLE_ID = "io.reproof.example.mixedshop"


def _make_app(products_dir: Path, name: str, bundle_id: str, *, binary: bool = False) -> Path:
    """products_dir 아래에 Info.plist가 있는 가짜 .app을 만든다."""
    app = products_dir / name
    app.mkdir(parents=True)
    fmt = plistlib.FMT_BINARY if binary else plistlib.FMT_XML
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}, fmt=fmt))
    return app


def _build_callback(counter: list):
    """xcodebuild build 응답: -derivedDataPath 아래에 가짜 앱을 만든다."""
    def respond(argv, cwd, env_extra):
        counter.append(argv)
        derived = Path(argv[argv.index("-derivedDataPath") + 1])
        _make_app(derived / PRODUCTS_SUBPATH, "MixedShop.app", BUNDLE_ID, binary=True)
        return ""
    return respond


class SimpleBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "회사 앱"
        self.source.mkdir()
        (self.source / "a.swift").write_text("let a = 1\n")
        self.workspace = self.tmp / "ws"
        self.config = dataclasses.replace(sample_config(), generator="none", generator_spec=None)
        self.builds = []

    def _runner(self, build_response=None):
        git_failure = ToolFailure("git", log_path=None, output_tail="not a git repository")
        return FakeToolRunner([
            (("git",), git_failure),
            (("xcodebuild", "-resolvePackageDependencies"), ""),
            (("xcodebuild", "build"), build_response or _build_callback(self.builds)),
        ])

    def test_build_argv_has_fixed_flags(self):
        argv = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"))
        for flag in ("-disableAutomaticPackageResolution", "-skipMacroValidation", "-skipPackagePluginValidation",
                     "COMPILER_INDEX_STORE_ENABLE=NO", "-onlyUsePackageVersionsFromResolvedFile"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-configuration") + 1], "Debug")
        self.assertEqual(argv[argv.index("-project") + 1], "MixedShop.xcodeproj")
        unpinned = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"), is_pinned=False)
        self.assertNotIn("-onlyUsePackageVersionsFromResolvedFile", unpinned)

    def test_build_argv_ad_hoc_signs_so_simulator_entitlements_are_embedded(self):
        """서명을 끄지 않고 인증서 없는 ad-hoc 서명(`-`)을 쓰는지 확인한다.

        왜: CODE_SIGNING_ALLOWED=NO면 entitlement(App Group 등)가 실행 파일에 들어가지 않아, App Group 컨테이너를 쓰는
        앱(Wikipedia: MWKDataStore)이 Simulator에서 시작하자마자 죽는다. ad-hoc은 키체인 인증서·프로필을 쓰지 않는다.
        """
        argv = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"))
        self.assertIn("CODE_SIGN_IDENTITY=-", argv)
        self.assertNotIn("CODE_SIGNING_ALLOWED=NO", argv)

    def test_find_app_bundle_matches_bundle_identifier(self):
        products = self.tmp / "products"
        _make_app(products, "Widget.app", "io.other.widget")
        wanted = _make_app(products, "Main.app", BUNDLE_ID, binary=True)
        self.assertEqual(find_app_bundle(products, BUNDLE_ID), wanted)
        with self.assertRaises(SimpleError) as missing:
            find_app_bundle(products, "io.none")
        self.assertEqual(missing.exception.code, "app_bundle_missing")
        _make_app(products, "Copy.app", BUNDLE_ID)
        with self.assertRaises(SimpleError) as ambiguous:
            find_app_bundle(products, BUNDLE_ID)
        self.assertEqual(ambiguous.exception.code, "app_bundle_ambiguous")

    def test_prepare_build_produces_documents_and_progress(self):
        messages = []
        runner = self._runner()
        prepared = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                                 progress=messages.append)
        self.assertEqual(messages[0], "빌드 식별 중")
        self.assertEqual(messages[1], "작업 사본 만드는 중")
        fixed = [m for m in messages if not m.startswith("Package.resolved")]
        self.assertEqual(fixed[2:4], ["패키지 받는 중", "앱 빌드 중"])
        self.assertTrue(any(m.startswith("Package.resolved가 없어") for m in messages))
        self.assertTrue(messages[-1].startswith("빌드 완료"))
        self.assertTrue((prepared.directory / "products.json").exists())
        self.assertTrue((prepared.directory / "identity.json").exists())
        self.assertTrue(prepared.products.app_path.is_dir())
        self.assertTrue(prepared.products.app_path.is_relative_to(prepared.directory))
        self.assertEqual(prepared.working_copy.root, prepared.directory / "source")
        self.assertTrue((prepared.working_copy.root / "a.swift").exists())
        self.assertEqual(prepared.directory, self.workspace / "projects" / "mixedshop" / "builds" / prepared.build_id)
        self.assertEqual(len(list(prepared.directory.parent.glob("*.partial"))), 0)
        self.assertEqual(list(prepared.directory.parent.parent.glob("staging-*")), [])

    def test_second_prepare_reuses_completed_build_without_xcodebuild(self):
        first = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                              progress=lambda _: None)
        runner = self._runner()
        reuse_messages = []
        second = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                               progress=reuse_messages.append)
        self.assertEqual(second.build_id, first.build_id)
        self.assertEqual(second.products, first.products)
        self.assertFalse([c for c in runner.calls if c["argv"][0] == "xcodebuild"])
        self.assertEqual(reuse_messages[-1], f"빌드 완료: {second.products.app_path}")
        self.assertEqual(list(first.directory.parent.parent.glob("staging-*")), [])

    def test_prepare_build_discards_partial_directory_from_interrupted_run(self):
        interrupted = self._runner(lambda argv, cwd, env: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=interrupted,
                          progress=lambda _: None)
        builds_dir = self.workspace / "projects" / "mixedshop" / "builds"
        self.assertEqual([p.name.endswith(".partial") for p in builds_dir.iterdir()], [True])
        runner = self._runner()
        prepared = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                                 progress=lambda _: None)
        self.assertEqual(len(self.builds), 1)
        self.assertTrue((prepared.directory / "products.json").exists())
        self.assertEqual([p.name for p in builds_dir.iterdir()], [prepared.build_id])

    def test_relative_workspace_root_builds_like_real_xcodebuild(self):
        def respond(argv, cwd, env_extra):
            derived = cwd / argv[argv.index("-derivedDataPath") + 1]  # 실제 xcodebuild처럼 cwd 기준으로 푼다
            _make_app(derived / PRODUCTS_SUBPATH, "MixedShop.app", BUNDLE_ID)
            return ""
        previous = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        prepared = prepare_build(self.source, self.config, workspace_root=Path("rel-ws"), runner=self._runner(respond),
                                 progress=lambda _: None)
        self.assertTrue(prepared.directory.is_absolute())
        self.assertTrue(prepared.products.app_path.is_dir())

    def test_pinned_build_skips_validation_unpinned_does_not(self):
        pinned = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"), is_pinned=True)
        unpinned = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"), is_pinned=False)
        for flag in ("-skipMacroValidation", "-skipPackagePluginValidation"):
            self.assertIn(flag, pinned)
            self.assertNotIn(flag, unpinned)

    def test_unpinned_validation_failure_maps_to_package_validation_required(self):
        failure = ToolFailure("xcodebuild", log_path=None,
                              output_tail="error: Macro \"M\" must be enabled before it can be used")
        def raise_failure(argv, cwd, env_extra):
            raise failure
        with self.assertRaises(SimpleError) as raised:
            prepare_build(self.source, self.config, workspace_root=self.workspace,
                          runner=self._runner(raise_failure), progress=lambda _: None)
        self.assertEqual(raised.exception.code, "package_validation_required")

    def test_corrupt_products_json_triggers_rebuild(self):
        first = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                              progress=lambda _: None)
        for content in ("null", "[]"):
            (first.directory / "products.json").write_text(content)
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                          progress=lambda _: None)
        self.assertEqual(len(self.builds), 3)

    def test_find_app_bundle_skips_plist_with_non_dict_root(self):
        products = self.tmp / "products"
        bad = products / "Bad.app"
        bad.mkdir(parents=True)
        (bad / "Info.plist").write_bytes(plistlib.dumps(["x"]))
        good = _make_app(products, "Good.app", BUNDLE_ID)
        self.assertEqual(find_app_bundle(products, BUNDLE_ID), good)

    def test_default_workspace_root_honors_reproof_home(self):
        self.assertEqual(default_workspace_root({"REPROOF_HOME": "/x/y"}), Path("/x/y"))
        self.assertEqual(default_workspace_root({}), Path.home() / ".reproof")


if __name__ == "__main__":
    unittest.main()
