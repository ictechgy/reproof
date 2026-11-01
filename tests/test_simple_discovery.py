"""간편 모드 프로젝트 탐색 계약. Xcode 없이 FakeToolRunner로 실행된다."""
import json
import tempfile
import unittest
from pathlib import Path

from reproof.simple.discovery import (AppSettings, ContainerScan, DiscoveryPaths, choose_container,
                                      guess_test_scheme, list_schemes, read_app_settings, scan_containers)
from reproof.simple.errors import SimpleError
from tests.simple_support import FakeToolRunner

APP_TYPE = "com.apple.product-type.application"
EXTENSION_TYPE = "com.apple.product-type.app-extension"


def build_settings_entry(target, product_type, bundle_id):
    """showBuildSettings JSON 배열의 항목 하나를 만든다."""
    return {"target": target, "buildSettings": {"TARGET_NAME": target, "PRODUCT_TYPE": product_type,
                                                  "PRODUCT_BUNDLE_IDENTIFIER": bundle_id}}


class SimpleDiscoveryTests(unittest.TestCase):
    def make_root(self, *names):
        """임시 디렉터리에 이름(끝이 /면 디렉터리, 아니면 파일)을 만들고 경로를 돌려준다."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in names:
            path = root / name
            if name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
        return root

    def test_xcodegen_spec_wins_and_is_reported(self):
        root = self.make_root("project.yml", "App.xcodeproj/")
        scan = scan_containers(root)
        self.assertEqual((scan.generator, scan.generator_spec), ("xcodegen", "project.yml"))
        self.assertIsNone(choose_container(scan))

    def test_tuist_and_xcodegen_together_is_ambiguous(self):
        root = self.make_root("project.yml", "Project.swift")
        with self.assertRaises(SimpleError) as caught:
            scan_containers(root)
        self.assertEqual(caught.exception.code, "generator_ambiguous")

    def test_tuist_markers_detected(self):
        root = self.make_root("Tuist/")
        scan = scan_containers(root)
        self.assertEqual(scan.generator, "tuist")

    def test_single_workspace_preferred_over_project(self):
        root = self.make_root("App.xcworkspace/", "App.xcodeproj/project.xcworkspace/",
                              "App.xcodeproj/", "Pods/Pods.xcodeproj/", "node_modules/x/Y.xcodeproj/",
                              "DerivedData/Z.xcworkspace/")
        scan = scan_containers(root)
        self.assertEqual(scan.workspaces, ("App.xcworkspace",))
        self.assertEqual(scan.projects, ("App.xcodeproj",))
        self.assertEqual(choose_container(scan), "App.xcworkspace")

    def test_nested_containers_found_within_depth_three(self):
        root = self.make_root("a/b/App.xcodeproj/", "a/b/c/d/Deep.xcodeproj/")
        self.assertEqual(scan_containers(root).projects, ("a/b/App.xcodeproj",))

    def test_single_project_is_chosen(self):
        scan = scan_containers(self.make_root("App.xcodeproj/"))
        self.assertEqual(choose_container(scan), "App.xcodeproj")

    def test_two_projects_without_workspace_is_ambiguous(self):
        scan = scan_containers(self.make_root("A.xcodeproj/", "B.xcodeproj/"))
        self.assertIsNone(choose_container(scan))

    def test_list_schemes_reads_workspace_and_project_json_shapes(self):
        workspace_json = "warning: noise\n" + json.dumps({"workspace": {"schemes": ["A", "B"]}})
        project_json = json.dumps({"project": {"schemes": ["C"]}})
        fake = FakeToolRunner([(("xcodebuild", "-list", "-json", "-workspace"), workspace_json),
                               (("xcodebuild", "-list", "-json", "-project"), project_json)])
        self.assertEqual(list_schemes(Path("/tmp"), "A.xcworkspace", fake), ("A", "B"))
        self.assertEqual(list_schemes(Path("/tmp"), "C.xcodeproj", fake), ("C",))

    def test_read_app_settings_ignores_app_extensions(self):
        entries = [build_settings_entry("Widget", EXTENSION_TYPE, "com.x.app.widget"),
                   build_settings_entry("App", APP_TYPE, "com.x.app")]
        fake = FakeToolRunner([(("xcodebuild",), "note\n" + json.dumps(entries))])
        settings = read_app_settings(Path("/tmp"), "App.xcodeproj", "App", fake)
        self.assertEqual(settings, AppSettings("App", "com.x.app"))

    def test_read_app_settings_prefers_target_named_like_scheme(self):
        entries = [build_settings_entry("Other", APP_TYPE, "com.x.other"),
                   build_settings_entry("App", APP_TYPE, "com.x.app")]
        fake = FakeToolRunner([(("xcodebuild",), json.dumps(entries))])
        self.assertEqual(read_app_settings(Path("/tmp"), "App.xcworkspace", "App", fake).bundle_id, "com.x.app")
        self.assertIn("-workspace", fake.calls[0]["argv"])

    def test_read_app_settings_ambiguous_and_missing(self):
        two = [build_settings_entry("A", APP_TYPE, "a"), build_settings_entry("B", APP_TYPE, "b")]
        none = [build_settings_entry("W", EXTENSION_TYPE, "w")]
        for entries, code in ((two, "app_target_ambiguous"), (none, "app_target_missing")):
            fake = FakeToolRunner([(("xcodebuild",), json.dumps(entries))])
            with self.assertRaises(SimpleError) as caught:
                read_app_settings(Path("/tmp"), "App.xcodeproj", "App", fake)
            self.assertEqual(caught.exception.code, code)

    def test_discovery_paths_keep_xcodebuild_out_of_global_derived_data(self):
        """탐색 폴더를 주면 -list·-showBuildSettings 모두 DerivedData와 패키지 폴더를 임시·작업 공간 폴더로 받는다.

        왜: 주지 않으면 Xcode가 ~/Library/Developer/Xcode/DerivedData에 임시 사본 기준 폴더(Wikipedia는 1.1 GB)를
        init마다 남긴다. -list는 -derivedDataPath를 거부하므로(`-scheme ... is required`) 같은 효과의 사용자 기본값
        `-IDECustomDerivedDataLocation=<폴더>`를 쓴다(Xcode 27.0에서 전역 폴더가 생기지 않음을 확인).
        """
        paths = DiscoveryPaths(derived_data=Path("/w/tmp/init-1/dd"), packages_dir=Path("/w/cache/spm/discovery"))
        entries = [build_settings_entry("App", APP_TYPE, "com.x.app")]
        fake = FakeToolRunner([(("xcodebuild", "-list"), json.dumps({"project": {"schemes": ["App"]}})),
                               (("xcodebuild", "-showBuildSettings"), json.dumps(entries))])
        list_schemes(Path("/tmp"), "App.xcodeproj", fake, paths=paths)
        read_app_settings(Path("/tmp"), "App.xcodeproj", "App", fake, paths=paths)
        list_argv, settings_argv = (call["argv"] for call in fake.calls)
        self.assertIn("-clonedSourcePackagesDirPath", list_argv)
        self.assertIn("-derivedDataPath", settings_argv)
        self.assertEqual(list_argv[list_argv.index("-clonedSourcePackagesDirPath") + 1], "/w/cache/spm/discovery")
        self.assertNotIn("-derivedDataPath", list_argv)
        self.assertIn("-IDECustomDerivedDataLocation=/w/tmp/init-1/dd", list_argv)
        self.assertEqual(settings_argv[settings_argv.index("-derivedDataPath") + 1], "/w/tmp/init-1/dd")
        self.assertEqual(settings_argv[settings_argv.index("-clonedSourcePackagesDirPath") + 1],
                         "/w/cache/spm/discovery")

    def test_discovery_without_paths_keeps_previous_argv(self):
        """탐색 폴더를 주지 않는 기존 호출자는 argv가 그대로다."""
        entries = [build_settings_entry("App", APP_TYPE, "com.x.app")]
        fake = FakeToolRunner([(("xcodebuild", "-list"), json.dumps({"project": {"schemes": ["App"]}})),
                               (("xcodebuild", "-showBuildSettings"), json.dumps(entries))])
        list_schemes(Path("/tmp"), "App.xcodeproj", fake)
        read_app_settings(Path("/tmp"), "App.xcodeproj", "App", fake)
        for call in fake.calls:
            self.assertNotIn("-derivedDataPath", call["argv"])
            self.assertNotIn("-clonedSourcePackagesDirPath", call["argv"])
            self.assertFalse([item for item in call["argv"] if item.startswith("-IDECustomDerivedDataLocation")])

    def test_argv_keeps_space_and_korean_container_path_as_single_argument(self):
        entries = [build_settings_entry("My App", APP_TYPE, "com.x.app")]
        fake = FakeToolRunner([(("xcodebuild",), json.dumps(entries))])
        read_app_settings(Path("/tmp/회사 앱"), "My App.xcodeproj", "My App", fake)
        self.assertIn("My App.xcodeproj", fake.calls[0]["argv"])
        self.assertEqual(fake.calls[0]["cwd"], Path("/tmp/회사 앱"))

    def test_guess_test_scheme_prefers_unit_tests_over_ui_tests(self):
        schemes = ("App", "AppUITests", "AppTests", "OtherTests")
        self.assertEqual(guess_test_scheme(schemes, "App"), "AppTests")
        self.assertIsNone(guess_test_scheme(("App", "AppUITests"), "App"))

    def test_guess_test_scheme_falls_back_to_sorted_first(self):
        self.assertEqual(guess_test_scheme(("App", "OtherTests", "BTests"), "App"), "BTests")
        self.assertEqual(guess_test_scheme(("ZTests", "AppZTests", "AppATests"), "App"), "AppATests")

    def test_guess_test_scheme_app_named_beats_earlier_sorted_candidate(self):
        self.assertEqual(guess_test_scheme(("App", "AAATests", "AppTests"), "App"), "AppTests")

    def assert_invalid_build_settings(self, entries):
        """주어진 showBuildSettings 배열이 build_settings_invalid 로 거부되는지 확인한다."""
        fake = FakeToolRunner([(("xcodebuild",), json.dumps(entries))])
        with self.assertRaises(SimpleError) as caught:
            read_app_settings(Path("/tmp"), "App.xcodeproj", "App", fake)
        self.assertEqual(caught.exception.code, "build_settings_invalid")

    def test_read_app_settings_missing_bundle_id_is_user_error(self):
        entry = build_settings_entry("App", APP_TYPE, "x")
        del entry["buildSettings"]["PRODUCT_BUNDLE_IDENTIFIER"]
        self.assert_invalid_build_settings([entry])

    def test_read_app_settings_missing_target_name_is_user_error(self):
        entry = build_settings_entry("App", APP_TYPE, "x")
        del entry["buildSettings"]["TARGET_NAME"]
        self.assert_invalid_build_settings([entry])

    def test_read_app_settings_non_dict_entry_is_user_error(self):
        self.assert_invalid_build_settings([build_settings_entry("App", APP_TYPE, "x"), "junk"])


if __name__ == "__main__":
    unittest.main()
