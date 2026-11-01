"""Simulator 빌드 argv, .app 탐색, 빌드 파이프라인(재사용·끊긴 빌드 폐기) 테스트. Xcode 없이 FakeToolRunner로 실행된다."""
import dataclasses
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reproof.simple.build import (BUILD_RECIPE_VERSION, PRODUCTS_SUBPATH, find_app_bundle, is_validation_failure,
                                  simulator_build_argv)
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

    def _runner(self, build_response=None, *, xcode_version="Xcode 16.0\nBuild version 16A"):
        git_failure = ToolFailure("git", log_path=None, output_tail="not a git repository")
        return FakeToolRunner([
            (("git",), git_failure),
            (("xcodebuild", "-version"), xcode_version + "\n"),
            (("xcodebuild", "-resolvePackageDependencies"), ""),
            (("xcodebuild", "build"), build_response or _build_callback(self.builds)),
        ])

    def test_build_argv_has_fixed_flags(self):
        argv = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"))
        for flag in ("-disableAutomaticPackageResolution", "COMPILER_INDEX_STORE_ENABLE=NO",
                     "-onlyUsePackageVersionsFromResolvedFile"):
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

    def test_credential_files_are_left_out_of_build_and_recorded(self):
        """비밀 가능 파일(.env)은 작업 사본에 넣지 않고, 알림·identity.json에 남기며, include로 지정하면 넣는다."""
        (self.source / ".env").write_text("TOKEN=SECRET\n")
        messages = []
        prepared = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                                 progress=messages.append)
        self.assertFalse(os.path.lexists(prepared.working_copy.root / ".env"))
        self.assertIn("제외함(비밀 가능 파일): .env", messages)
        document = json.loads((prepared.directory / "identity.json").read_text(encoding="utf-8"))
        self.assertEqual(document["excludedPaths"], [".env"])
        included = prepare_build(self.source, dataclasses.replace(self.config, source_include=(".env",)),
                                 workspace_root=self.workspace, runner=self._runner(), progress=lambda _: None)
        self.assertTrue((included.working_copy.root / ".env").exists())
        self.assertNotEqual(included.build_id, prepared.build_id)

    def test_second_prepare_reuses_completed_build_without_xcodebuild(self):
        first = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                              progress=lambda _: None)
        runner = self._runner()
        reuse_messages = []
        second = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                               progress=reuse_messages.append)
        self.assertEqual(second.build_id, first.build_id)
        self.assertEqual(second.products, first.products)
        self.assertEqual([c["argv"] for c in runner.calls if c["argv"][0] == "xcodebuild"],
                         [["xcodebuild", "-version"]])  # 빌드 ID 계산용 버전 확인만 하고 빌드는 하지 않는다.
        self.assertEqual(reuse_messages[-1], f"빌드 완료: {second.products.app_path}")
        self.assertEqual(list(first.directory.parent.parent.glob("staging-*")), [])

    def test_new_xcode_or_build_recipe_does_not_reuse_completed_build(self):
        """Xcode 버전이나 빌드 방법 버전이 바뀌면 완성 빌드를 재사용하지 않고 새로 빌드한다."""
        first = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                              progress=lambda _: None)
        newer_xcode = prepare_build(self.source, self.config, workspace_root=self.workspace,
                                    runner=self._runner(xcode_version="Xcode 99.0\nBuild version 99A"),
                                    progress=lambda _: None)
        with mock.patch("reproof.simple.pipeline.BUILD_RECIPE_VERSION", BUILD_RECIPE_VERSION + 1):
            newer_recipe = prepare_build(self.source, self.config, workspace_root=self.workspace,
                                         runner=self._runner(), progress=lambda _: None)
        self.assertEqual(len(self.builds), 3)
        self.assertEqual(len({first.build_id, newer_xcode.build_id, newer_recipe.build_id}), 3)
        document = json.loads((first.directory / "identity.json").read_text(encoding="utf-8"))
        self.assertEqual(document["buildRecipe"], BUILD_RECIPE_VERSION)
        self.assertEqual(document["xcodeVersion"], "Xcode 16.0\nBuild version 16A")

    def test_missing_xcode_version_stops_before_copying_source(self):
        """`xcodebuild -version`이 실패하면 작업 사본을 만들기 전에 xcode_version_unavailable로 멈춘다."""
        runner = FakeToolRunner([(("xcodebuild", "-version"), ToolFailure("xcodebuild", log_path=None, output_tail=""))])
        with self.assertRaises(SimpleError) as raised:
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                          progress=lambda _: None)
        self.assertEqual(raised.exception.code, "xcode_version_unavailable")
        self.assertFalse((self.workspace / "projects").exists())

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

    def test_macro_and_plugin_validation_is_skipped_only_when_opted_in(self):
        """기본은 고정 여부와 상관없이 매크로·플러그인 검증을 건너뛰지 않고, [build] skip_package_validation = true일 때만
        건너뛴다.

        왜: Package.resolved가 있다는 것은 개발자가 그 매크로·플러그인(빌드 중 임의 코드 실행)을 신뢰했다는 뜻이 아니다.
        처음 받는 의존성이나 자동으로 갱신된 resolved도 Xcode의 신뢰 확인 없이 실행되면 안 된다.
        """
        flags = ("-skipMacroValidation", "-skipPackagePluginValidation")
        opted_in = dataclasses.replace(self.config, skip_package_validation=True)
        for is_pinned in (True, False):
            with self.subTest(is_pinned=is_pinned):
                default = simulator_build_argv(self.config, derived_data=Path("/d"), packages_dir=Path("/p"),
                                               is_pinned=is_pinned)
                skipped = simulator_build_argv(opted_in, derived_data=Path("/d"), packages_dir=Path("/p"),
                                               is_pinned=is_pinned)
                for flag in flags:
                    self.assertNotIn(flag, default)
                    self.assertIn(flag, skipped)

    def test_validation_failure_maps_to_package_validation_required_even_when_pinned(self):
        """검증 실패 문구는 고정된 빌드에서도 package_validation_required로 바꾸고, Xcode 신뢰와 설정 두 방법을 안내한다."""
        failure = ToolFailure("xcodebuild", log_path=None,
                              output_tail="error: Macro \"M\" must be enabled before it can be used")
        def raise_failure(argv, cwd, env_extra):
            raise failure
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                if pinned:
                    resolved = (self.source / "MixedShop.xcodeproj" / "project.xcworkspace" / "xcshareddata" /
                                "swiftpm" / "Package.resolved")
                    resolved.parent.mkdir(parents=True)
                    resolved.write_text('{"pins": []}')
                with self.assertRaises(SimpleError) as raised:
                    prepare_build(self.source, self.config, workspace_root=self.workspace,
                                  runner=self._runner(raise_failure), progress=lambda _: None)
                self.assertEqual(raised.exception.code, "package_validation_required")
                self.assertIn("Xcode", raised.exception.remedy)
                self.assertIn("skip_package_validation", raised.exception.remedy)

    def test_only_specific_marker_counts_as_validation_failure(self):
        """"must be enabled before it can be used" 문구만 검증 실패로 보고, macro·trust·enable 낱말 조합은 보지 않는지 본다.

        왜: 넓은 낱말 조합은 일반 빌드 오류(예: trust store, enable 옵션 안내)를 검증 문제로 잘못 안내했다.
        """
        def failure(text):
            return ToolFailure("xcodebuild", log_path=None, output_tail=text)
        self.assertTrue(is_validation_failure(failure('error: Macro "M" must be enabled before it can be used')))
        self.assertFalse(is_validation_failure(failure(
            "warning: plugin output; enable trust for the keychain macro step failed")))

    def test_build_timeout_points_to_build_timeout_setting(self):
        """빌드가 시간 제한을 넘기면 reproof.toml의 [build] timeout_seconds를 늘리라고 안내하는지 본다."""
        failure = ToolFailure("xcodebuild", log_path=None, output_tail="", reason="실행이 시간 제한 3600초를 넘겼습니다.",
                              timeout_seconds=3600)
        def raise_failure(argv, cwd, env_extra):
            raise failure
        with self.assertRaises(ToolFailure) as raised:
            prepare_build(self.source, self.config, workspace_root=self.workspace,
                          runner=self._runner(raise_failure), progress=lambda _: None)
        self.assertIn("시간 제한 3600초", raised.exception.cause)
        self.assertIn("timeout_seconds", raised.exception.remedy)

    def test_corrupt_products_json_triggers_rebuild(self):
        first = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                              progress=lambda _: None)
        for content in ("null", "[]"):
            (first.directory / "products.json").write_text(content)
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                          progress=lambda _: None)
        self.assertEqual(len(self.builds), 3)

    def _first_build(self):
        """첫 빌드를 만들고 PreparedBuild를 돌려준다."""
        return prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                             progress=lambda _: None)

    def _rebuilds_after(self, change) -> bool:
        """첫 빌드 뒤 change(prepared)로 완성 빌드를 바꾸고, 다음 prepare_build가 다시 빌드했는지 돌려준다."""
        first = self._first_build()
        change(first)
        before = len(self.builds)
        second = prepare_build(self.source, self.config, workspace_root=self.workspace, runner=self._runner(),
                               progress=lambda _: None)
        self.assertEqual(second.build_id, first.build_id)
        return len(self.builds) == before + 1

    def _rewrite_products(self, prepared, **changes):
        """완성 빌드의 products.json 값을 바꿔 쓴다."""
        path = prepared.directory / "products.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**document, **changes}), encoding="utf-8")

    def test_tampered_app_is_not_reused(self):
        """완성 빌드의 .app 내용이 기록된 products_digest와 다르면 재사용하지 않고 다시 빌드한다."""
        self.assertTrue(self._rebuilds_after(
            lambda prepared: (prepared.products.app_path / "injected.dylib").write_bytes(b"evil")))

    def test_products_paths_outside_build_directory_are_not_reused(self):
        """products.json의 app_path·derived_data가 절대 경로이거나 빌드 폴더 밖('..')을 가리키면 재사용하지 않는다."""
        outside = _make_app(self.tmp / "outside", "Other.app", BUNDLE_ID)
        cases = [{"app_path": str(outside)}, {"app_path": "../../../../outside/Other.app"},
                 {"derived_data": "/tmp"}, {"derived_data": "../.."}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertTrue(self._rebuilds_after(lambda prepared: self._rewrite_products(prepared, **changes)))

    def test_bundle_id_mismatch_is_not_reused(self):
        """products.json의 bundle_id가 설정과 다르면 재사용하지 않는다."""
        self.assertTrue(self._rebuilds_after(lambda prepared: self._rewrite_products(prepared, bundle_id="io.other")))

    def test_workspace_directories_are_private(self):
        """새 작업 공간과 도구가 만드는 projects·builds·cache 폴더는 0700이다. 기존 도구 폴더도 0700으로 좁힌다.

        왜: 빌드 폴더에는 회사 앱 소스 전체 사본·dirty.patch·화면 계층이 들어, 같은 Mac의 다른 계정이 읽으면 안 된다.
        """
        prepared = self._first_build()
        folders = (self.workspace, self.workspace / "projects", self.workspace / "projects" / "mixedshop",
                   prepared.directory.parent, self.workspace / "cache", self.workspace / "cache" / "spm",
                   self.workspace / "runner", self.workspace / "logs", self.workspace / "tmp")
        for folder in folders[1:]:
            os.chmod(folder, 0o755)  # 이전 버전이 umask 권한으로 만든 도구 폴더를 흉내 낸다.
        self._first_build()
        for folder in folders:
            with self.subTest(folder=folder):
                self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)

    def test_existing_workspace_root_mode_is_left_alone(self):
        """사용자가 지정한 기존 작업 공간 폴더 자체의 권한은 바꾸지 않는다(도구가 만든 하위 폴더만 좁힌다)."""
        self.workspace.mkdir()
        os.chmod(self.workspace, 0o755)
        self._first_build()
        self.assertEqual(os.stat(self.workspace).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(self.workspace / "projects").st_mode & 0o777, 0o700)

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
