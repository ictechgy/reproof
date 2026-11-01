"""간편 모드 CLI(init·doctor·build·inspect) 계약. Xcode 없이 FakeToolRunner와 input patch로 실행된다."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from unittest import mock

from reproof.simple.cli import find_config, main
from reproof.simple.errors import ToolFailure
from reproof.simple.config import CONFIG_FILE_NAME, load_config, render_config
from tests.simple_support import FakeToolRunner, sample_config

APP_TYPE = "com.apple.product-type.application"
TEST_TYPE = "com.apple.product-type.bundle.unit-test"
DiskUsage = namedtuple("DiskUsage", "total used free")


def settings_json(target, product_type, bundle_id):
    """xcodebuild -showBuildSettings -json 출력 한 개를 만든다."""
    return json.dumps([{"target": target, "buildSettings": {
        "TARGET_NAME": target, "PRODUCT_TYPE": product_type, "PRODUCT_BUNDLE_IDENTIFIER": bundle_id}}])


def scheme_responses(app_schemes, other_schemes=()):
    """-list와 scheme별 -showBuildSettings 응답을 만든다. other_schemes는 앱 타깃이 없는 scheme이다."""
    names = list(app_schemes) + list(other_schemes)
    responses = [(["xcodebuild", "-list", "-json"], json.dumps({"project": {"schemes": names}}))]
    for name in app_schemes:
        prefix = ["xcodebuild", "-showBuildSettings", "-json", "-scheme", name]
        responses.append((prefix, settings_json(name, APP_TYPE, f"com.example.{name.lower()}")))
    for name in other_schemes:
        prefix = ["xcodebuild", "-showBuildSettings", "-json", "-scheme", name]
        responses.append((prefix, settings_json(name, TEST_TYPE, f"com.example.{name.lower()}")))
    return responses


def doctor_responses(runtimes):
    """doctor가 부르는 xcodebuild -version과 simctl runtimes 응답을 만든다."""
    return [(["xcodebuild", "-version"], "Xcode 16.0\nBuild version 16A"),
            (["xcrun", "simctl", "list", "runtimes", "--json"], json.dumps({"runtimes": runtimes}))]


READY_RUNTIME = [{"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-0", "name": "iOS 18.0",
                  "version": "18.0", "isAvailable": True}]


class SimpleCliTests(unittest.TestCase):
    def setUp(self):
        """임시 폴더(공백·한글 포함)와 REPROOF_HOME을 준비하고, 출력과 실행기를 가로챈다."""
        tmp = tempfile.TemporaryDirectory(prefix="회사 앱 ")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.app = self.tmp / "My App"
        (self.app / "MixedShop.xcodeproj").mkdir(parents=True)
        environment = mock.patch.dict(os.environ, {"REPROOF_HOME": str(self.tmp / "home")})
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, argv, responses, inputs=()):
        """FakeToolRunner와 input 응답을 주입해 main을 실행하고 (코드, stdout, stderr, 실행기, input mock)을 돌려준다."""
        runner = FakeToolRunner(responses)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("reproof.simple.cli._new_runner", return_value=runner), \
                mock.patch("builtins.input", side_effect=list(inputs)) as ask, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue(), runner, ask

    def init_args(self, *extra):
        """init 인자 목록을 만든다."""
        return ["init", "--path", str(self.app), *extra]

    def test_init_writes_config_without_questions_for_single_choices(self):
        responses = scheme_responses(["MixedShop"], ["MixedShopTests"]) + doctor_responses(READY_RUNTIME)
        code, out, _, _, ask = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 0)
        config = load_config(self.app / CONFIG_FILE_NAME)
        self.assertEqual((config.scheme, config.container, config.test_scheme, config.name),
                         ("MixedShop", "MixedShop.xcodeproj", "MixedShopTests", "my-app"))
        self.assertEqual(config.bundle_id, "com.example.mixedshop")
        self.assertEqual(ask.call_count, 0)
        self.assertIn("질문 0개", out)
        self.assertIn("다음 단계: reproof app build", out)

    def test_init_asks_only_for_ambiguous_scheme(self):
        responses = scheme_responses(["Alpha", "Beta"]) + doctor_responses(READY_RUNTIME)
        code, out, _, _, ask = self.run_cli(self.init_args(), responses, inputs=["2"])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(load_config(self.app / CONFIG_FILE_NAME).scheme, "Beta")
        self.assertIn("질문 1개", out)

    def test_init_non_interactive_lists_choices_when_ambiguous(self):
        code, _, err, _, ask = self.run_cli(self.init_args("--non-interactive"), scheme_responses(["Alpha", "Beta"]))
        self.assertEqual(code, 2)
        self.assertIn("Alpha", err)
        self.assertIn("Beta", err)
        self.assertEqual(ask.call_count, 0)
        self.assertFalse((self.app / CONFIG_FILE_NAME).exists())

    def test_init_refuses_overwrite_without_force(self):
        existing = self.app / CONFIG_FILE_NAME
        existing.write_text("기존 내용", encoding="utf-8")
        code, _, err, _, _ = self.run_cli(self.init_args("--non-interactive"), [])
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "기존 내용")

    def test_init_force_overwrites(self):
        (self.app / CONFIG_FILE_NAME).write_text("기존 내용", encoding="utf-8")
        responses = scheme_responses(["MixedShop"]) + doctor_responses(READY_RUNTIME)
        code, *_ = self.run_cli(self.init_args("--non-interactive", "--force"), responses)
        self.assertEqual(code, 0)
        self.assertEqual(load_config(self.app / CONFIG_FILE_NAME).scheme, "MixedShop")

    def test_init_generator_project_generates_in_temp_copy_and_cleans_up(self):
        (self.app / "MixedShop.xcodeproj").rmdir()
        (self.app / "project.yml").write_text("name: MixedShop\n", encoding="utf-8")

        def generate(argv, cwd, env_extra):
            """xcodegen 대역: 작업 사본에 프로젝트를 만든다."""
            self.assertNotEqual(cwd, self.app)
            (cwd / "MixedShop.xcodeproj").mkdir()
            return ""

        responses = [(["xcodegen"], generate)] + scheme_responses(["MixedShop"]) + doctor_responses(READY_RUNTIME)
        with mock.patch("reproof.simple.generation.generator_tool_path", return_value=Path("/bin/xcodegen")):
            code, *_ = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 0)
        config = load_config(self.app / CONFIG_FILE_NAME)
        self.assertEqual((config.generator, config.container), ("xcodegen", "MixedShop.xcodeproj"))
        self.assertFalse((self.app / "MixedShop.xcodeproj").exists())
        self.assertEqual(list((self.tmp / "home" / "tmp").iterdir()), [])

    def test_init_skips_scheme_whose_settings_cannot_be_read(self):
        responses = [(["xcodebuild", "-showBuildSettings", "-json", "-scheme", "Broken"],
                      ToolFailure("xcodebuild", log_path=None, output_tail=""))]
        responses += scheme_responses(["MixedShop"], []) + doctor_responses(READY_RUNTIME)
        responses[1] = (responses[1][0], json.dumps({"project": {"schemes": ["Broken", "MixedShop"]}}))
        code, out, *_ = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 0)
        self.assertEqual(load_config(self.app / CONFIG_FILE_NAME).scheme, "MixedShop")
        self.assertIn("건너뜀: Broken (xcodebuild 실행이 실패했습니다.)", out)

    def test_init_reports_read_failure_when_no_scheme_is_usable(self):
        responses = [(["xcodebuild", "-list", "-json"], json.dumps({"project": {"schemes": ["Broken"]}})),
                     (["xcodebuild", "-showBuildSettings"], ToolFailure("xcodebuild", log_path=None, output_tail=""))]
        code, _, err, _, _ = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 2)
        self.assertIn("xcodebuild 실행이 실패", err)

    def tree_files(self):
        """앱 폴더 아래 모든 파일·폴더의 상대 경로와 파일 내용을 모은다(원본이 바뀌었는지 비교용)."""
        return {path.relative_to(self.app).as_posix(): path.read_bytes() if path.is_file() else None
                for path in self.app.rglob("*")}

    def test_init_never_runs_xcodebuild_in_the_repository(self):
        before = self.tree_files()
        responses = scheme_responses(["MixedShop"], ["MixedShopTests"]) + doctor_responses(READY_RUNTIME)
        code, _, _, runner, _ = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 0)
        xcodebuild_dirs = [call["cwd"] for call in runner.calls if call["argv"][0] == "xcodebuild" and "-list" in call["argv"]]
        self.assertTrue(xcodebuild_dirs)
        self.assertTrue(all(self.app not in (cwd, *cwd.parents) for cwd in xcodebuild_dirs))
        after = self.tree_files()
        self.assertIn(CONFIG_FILE_NAME, after)
        del after[CONFIG_FILE_NAME]
        self.assertEqual(after, before)
        self.assertEqual(list((self.tmp / "home" / "tmp").iterdir()), [])

    def test_init_refuses_symlinked_config_and_keeps_target(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("건드리면 안 됨", encoding="utf-8")
        (self.app / CONFIG_FILE_NAME).symlink_to(victim)
        for extra in ((), ("--force",)):
            code, _, err, _, _ = self.run_cli(self.init_args("--non-interactive", *extra), [])
            self.assertEqual(code, 2)
            self.assertIn("심볼릭 링크", err)
            self.assertEqual(victim.read_text(encoding="utf-8"), "건드리면 안 됨")

    def test_init_write_replaces_atomically_without_leftover_temp_files(self):
        responses = scheme_responses(["MixedShop"]) + doctor_responses(READY_RUNTIME)
        self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(sorted(path.name for path in self.app.iterdir()), ["MixedShop.xcodeproj", CONFIG_FILE_NAME])

    def test_config_discovery_stops_at_git_root_and_home(self):
        outer = self.tmp / "outer"
        repo = outer / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        (outer / CONFIG_FILE_NAME).write_text("x", encoding="utf-8")
        self.assertIsNone(find_config(repo / "sub"))
        (repo / CONFIG_FILE_NAME).write_text("x", encoding="utf-8")
        self.assertEqual(find_config(repo / "sub"), repo / CONFIG_FILE_NAME)
        home = self.tmp / "home-user"
        (home / "work").mkdir(parents=True)
        (self.tmp / CONFIG_FILE_NAME).write_text("x", encoding="utf-8")
        with mock.patch("pathlib.Path.home", return_value=home):
            self.assertIsNone(find_config(home / "work"))

    def test_config_owned_by_another_user_is_refused(self):
        path = self.write_config()
        with mock.patch("reproof.simple.cli._current_uid", return_value=os.stat(path).st_uid + 1):
            code, _, err, runner, _ = self.run_cli(["doctor", "--config", str(path)], [])
        self.assertEqual(code, 2)
        self.assertIn("소유자", err)
        self.assertEqual(runner.calls, [])

    def test_commands_print_which_config_file_is_used(self):
        path = self.write_config()
        _, out, *_ = self.run_cli(["doctor", "--config", str(path)], doctor_responses(READY_RUNTIME))
        self.assertIn(f"설정 파일: {path}", out)

    def write_config(self, **changes):
        """생성기 없는 sample_config를 임시 폴더에 쓰고 경로를 돌려준다."""
        path = self.tmp / CONFIG_FILE_NAME
        path.write_text(render_config(replace(sample_config(), generator="none", generator_spec=None, **changes)),
                        encoding="utf-8")
        return path

    def test_doctor_reports_missing_runtime_with_download_command(self):
        path = self.write_config()
        code, out, _, _, _ = self.run_cli(["doctor", "--config", str(path)], doctor_responses([]))
        self.assertEqual(code, 2)
        self.assertIn("xcodebuild -downloadPlatform iOS", out)
        self.assertIn("✗ ios_runtime", out)
        self.assertIn("✓ xcode", out)

    def test_doctor_warns_when_disk_below_30gb(self):
        path = self.write_config()
        with mock.patch("shutil.disk_usage", return_value=DiskUsage(10**12, 10**12 - 10**10, 10**10)):
            code, out, _, _, _ = self.run_cli(["doctor", "--config", str(path)], doctor_responses(READY_RUNTIME))
        self.assertEqual(code, 2)
        self.assertIn("✗ disk", out)

    def test_doctor_passes_and_claude_is_informational(self):
        path = self.write_config()
        with mock.patch("shutil.disk_usage", return_value=DiskUsage(10**12, 0, 10**12)), \
                mock.patch("shutil.which", return_value=None):
            code, out, _, _, _ = self.run_cli(["doctor", "--config", str(path)], doctor_responses(READY_RUNTIME))
        self.assertIn("ⓘ claude", out)
        self.assertEqual(code, 2)  # git이 없어서 실패하므로 claude와 무관하게 2
        with mock.patch("shutil.disk_usage", return_value=DiskUsage(10**12, 0, 10**12)), \
                mock.patch("shutil.which", side_effect=lambda name: None if name == "claude" else "/usr/bin/git"):
            code, out, _, _, _ = self.run_cli(["doctor", "--config", str(path)], doctor_responses(READY_RUNTIME))
        self.assertEqual(code, 0)
        self.assertIn("ⓘ claude", out)

    def test_simple_error_is_printed_in_korean_and_returns_2(self):
        code, out, err, _, _ = self.run_cli(["inspect", "--config", str(self.tmp / "없음.toml")], [])
        self.assertEqual(code, 2)
        self.assertIn("문제:", err)
        self.assertIn("해결:", err)
        self.assertEqual(out, "")

    def test_inspect_rejects_invalid_point_before_building(self):
        path = self.write_config()
        code, _, err, runner, _ = self.run_cli(["inspect", "--config", str(path), "--point", "abc"], [])
        self.assertEqual(code, 2)
        self.assertIn("X,Y", err)
        self.assertEqual(runner.calls, [])

    def test_build_without_config_explains_init(self):
        with mock.patch("pathlib.Path.cwd", return_value=self.tmp):
            code, _, err, _, _ = self.run_cli(["build"], [])
        self.assertEqual(code, 2)
        self.assertIn("reproof app init", err)


if __name__ == "__main__":
    unittest.main()
