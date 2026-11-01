"""간편 모드 CLI(init·doctor·build·inspect) 계약. Xcode 없이 FakeToolRunner와 input patch로 실행된다."""
import contextlib
import errno
import io
import json
import os
import stat
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

    def test_init_does_not_overwrite_config_that_appears_during_discovery(self):
        """탐색(수 분 걸릴 수 있음) 도중 다른 사람·다른 init이 reproof.toml을 만들었으면 --force 없이 덮어쓰지 않는다."""
        existing = self.app / CONFIG_FILE_NAME

        def list_and_write(argv, cwd, env_extra):
            existing.write_text("다른 사람이 쓴 설정", encoding="utf-8")
            return json.dumps({"project": {"schemes": ["MixedShop"]}})

        responses = [(["xcodebuild", "-list", "-json"], list_and_write)] + scheme_responses(["MixedShop"])[1:]
        code, _, err, _, _ = self.run_cli(self.init_args("--non-interactive"), responses + doctor_responses(READY_RUNTIME))
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(existing.read_text(encoding="utf-8"), "다른 사람이 쓴 설정")
        self.assertEqual(sorted(path.name for path in self.app.iterdir()), ["MixedShop.xcodeproj", CONFIG_FILE_NAME])

    def test_init_falls_back_to_exclusive_create_when_hard_links_are_unsupported(self):
        """하드 링크를 못 만드는 파일 시스템(ENOTSUP·EPERM·EXDEV)에서는 O_EXCL 새 파일로 설정을 만든다. 이미 있으면 덮어쓰지 않는다."""
        responses = scheme_responses(["MixedShop"]) + doctor_responses(READY_RUNTIME)
        target = self.app / CONFIG_FILE_NAME
        for number in (errno.ENOTSUP, errno.EPERM, errno.EXDEV):
            with self.subTest(errno=errno.errorcode[number]):
                target.unlink(missing_ok=True)
                with mock.patch("reproof.simple.cli.os.link", side_effect=OSError(number, os.strerror(number))):
                    code, _, err, _, _ = self.run_cli(self.init_args("--non-interactive"), responses)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(load_config(target).scheme, "MixedShop")
                self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)
                self.assertEqual(sorted(path.name for path in self.app.iterdir()), ["MixedShop.xcodeproj", CONFIG_FILE_NAME])
        from reproof.simple.cli import _place_config
        from reproof.simple.errors import SimpleError
        temp = self.app / ".reproof-test.tmp"
        temp.write_text("x", encoding="utf-8")
        with mock.patch("reproof.simple.cli.os.link", side_effect=OSError(errno.ENOTSUP, "nope")), \
                self.assertRaises(SimpleError) as raised:
            _place_config(temp, target, is_forced=False)
        self.assertEqual(raised.exception.code, "config_exists")
        self.assertEqual(load_config(target).scheme, "MixedShop")
        temp.unlink()  # 실패 때 임시 파일 정리는 호출자(_write_config)가 한다.

    def test_init_takes_project_lock(self):
        """init은 같은 프로젝트 이름의 잠금을 쥔다. 다른 실행이 그 프로젝트를 쓰는 중이면 xcodebuild 전에 project_busy로 멈춘다."""
        import fcntl
        lock = self.tmp / "home" / "projects" / "my-app" / ".lock"
        lock.parent.mkdir(parents=True)
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, err, runner, _ = self.run_cli(self.init_args("--non-interactive"), [])
        self.assertEqual(code, 2)
        self.assertIn("같은 프로젝트(my-app)", err)
        self.assertEqual(runner.calls, [])

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

    def test_init_generate_failure_mentions_include_when_files_were_excluded(self):
        """init의 생성기 실행이 실패했고 작업 사본에서 비밀 가능 파일을 뺐다면 [source] include 안내를 덧붙인다."""
        (self.app / "MixedShop.xcodeproj").rmdir()
        (self.app / "project.yml").write_text("name: MixedShop\n", encoding="utf-8")
        (self.app / ".env").write_text("A=1\n", encoding="utf-8")
        failure = ToolFailure("xcodegen", log_path=None, output_tail="Spec references missing file .env")
        with mock.patch("reproof.simple.generation.generator_tool_path", return_value=Path("/bin/xcodegen")):
            code, out, err, _, _ = self.run_cli(self.init_args("--non-interactive"), [(["xcodegen"], failure)])
        self.assertEqual(code, 2)
        self.assertIn("제외함(비밀 가능 파일): .env", out)
        self.assertIn("[source] include", err)

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

    def test_init_keeps_discovery_xcodebuild_inside_workspace(self):
        """init의 -list·-showBuildSettings가 작업 공간 안의 패키지 폴더·임시 DerivedData를 쓰고, 임시 폴더는 지워진다.

        왜: 지정하지 않으면 ~/Library/Developer/Xcode/DerivedData에 init마다 고아 폴더(패키지 포함 1 GB 이상)가 남는다.
        """
        responses = scheme_responses(["MixedShop"], ["MixedShopTests"]) + doctor_responses(READY_RUNTIME)
        code, _, _, runner, _ = self.run_cli(self.init_args("--non-interactive"), responses)
        self.assertEqual(code, 0)
        home = self.tmp / "home"
        discovery = [call["argv"] for call in runner.calls
                     if call["argv"][:2] in (["xcodebuild", "-list"], ["xcodebuild", "-showBuildSettings"])]
        self.assertEqual(len(discovery), 3)
        for argv in discovery:
            self.assertIn("-clonedSourcePackagesDirPath", argv)
            self.assertEqual(Path(argv[argv.index("-clonedSourcePackagesDirPath") + 1]),
                             home / "cache" / "spm" / "discovery")
        locations = [item.split("=", 1)[1] for item in discovery[0] if item.startswith("-IDECustomDerivedDataLocation=")]
        locations += [argv[argv.index("-derivedDataPath") + 1] for argv in discovery[1:]]
        self.assertEqual(len(locations), 3)
        self.assertTrue(all(Path(location).is_relative_to(home / "tmp") for location in locations))
        self.assertEqual(list((home / "tmp").iterdir()), [])

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

    def test_symlinked_config_is_refused_even_if_target_is_owned_by_user(self):
        """--config나 자동 탐색이 찾은 reproof.toml이 링크면 소유자 검사 전에 config_symlink로 거절한다.

        왜: stat()은 링크를 따라가 대상의 소유자를 보므로, 다른 사람의 저장소가 내 다른 설정을 가리키게 할 수 있다.
        """
        real = self.tmp / "elsewhere.toml"
        real.write_text(render_config(replace(sample_config(), generator="none", generator_spec=None)),
                        encoding="utf-8")
        link = self.app / CONFIG_FILE_NAME
        link.symlink_to(real)
        for argv in (["doctor", "--config", str(link)],):
            code, _, err, runner, _ = self.run_cli(argv, doctor_responses(READY_RUNTIME))
            self.assertEqual(code, 2)
            self.assertIn("심볼릭 링크", err)
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

    def test_inspect_reports_missing_env_reference_before_building(self):
        """launch.environment의 env: 참조가 없으면 빌드·Simulator 부팅 전에 env_missing으로 멈추는지 본다."""
        path = self.write_config()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REPROOF_APP_API_TOKEN", None)
            code, _, err, runner, _ = self.run_cli(["inspect", "--config", str(path)], [])
        self.assertEqual(code, 2)
        self.assertIn("REPROOF_APP_API_TOKEN", err)
        self.assertEqual(runner.calls, [])

    def test_build_without_config_explains_init(self):
        with mock.patch("pathlib.Path.cwd", return_value=self.tmp):
            code, _, err, _, _ = self.run_cli(["build"], [])
        self.assertEqual(code, 2)
        self.assertIn("reproof app init", err)

    def write_app_config(self):
        """생성기 없는 sample_config를 앱 폴더(self.app)에 쓰고 경로를 돌려준다(build가 이 폴더를 복사한다)."""
        path = self.app / CONFIG_FILE_NAME
        path.write_text(render_config(replace(sample_config(), generator="none", generator_spec=None)),
                        encoding="utf-8")
        return path

    def test_unreadable_source_file_is_reported_in_korean_without_traceback(self):
        """앱 폴더에 읽을 수 없는 파일이 있으면 트레이스백 대신 원인·파일 이름·해결을 한국어로 알리고 2를 돌려주는지 본다."""
        path = self.write_app_config()
        secret = self.app / "Secret.swift"
        secret.write_text("let value = 1\n", encoding="utf-8")
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        code, _, err, _, _ = self.run_cli(["build", "--config", str(path)],
                                          [(["xcodebuild", "-version"], "Xcode 27.0\nBuild version 27A")])
        self.assertEqual(code, 2)
        self.assertIn("문제:", err)
        self.assertIn(str(secret), err)
        self.assertIn("권한", err)
        self.assertNotIn("Traceback", err)

    def test_disk_full_is_reported_with_free_space_remedy(self):
        """디스크가 가득 찬 OSError(ENOSPC)는 파일 이름과 함께 여유 공간 확보를 안내하는지 본다."""
        path = self.write_app_config()
        full = OSError(errno.ENOSPC, "No space left on device", "/작업/builds/abc.partial")
        with mock.patch("reproof.simple.cli.prepare_build", side_effect=full):
            code, _, err, _, _ = self.run_cli(["build", "--config", str(path)], [])
        self.assertEqual(code, 2)
        self.assertIn("No space left on device", err)
        self.assertIn("/작업/builds/abc.partial", err)
        self.assertIn("여유 공간", err)

    def test_hierarchy_json_is_written_owner_only_and_not_through_symlink(self):
        """화면 계층 JSON은 새 0600 파일로 써서 바꿔 넣는다. 저장 위치가 심볼릭·하드 링크여도 연결된 다른 파일은 그대로다.

        왜: 계층에는 로그인한 회사 앱 화면의 모든 글자(개인 정보·토큰 표시 등)가 들어 있고, 기존 inode를 열어 자르면
        하드 링크로 연결된 다른 파일까지 지워진다.
        """
        from reproof.simple.cli import save_hierarchy
        client = mock.Mock()
        client.command.return_value = {"ok": True, "root": {
            "type": "application", "identifier": "", "label": "A", "value": "", "placeholder": "",
            "frame": [0, 0, 10, 10], "enabled": True, "selected": False, "children": []}}
        output = self.tmp / "계층.json"
        output.write_text("{}", encoding="utf-8")
        os.chmod(output, 0o644)
        with contextlib.redirect_stdout(io.StringIO()):
            save_hierarchy(client, output)
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
        victim = self.tmp / "victim.txt"
        victim.write_text("건드리면 안 됨", encoding="utf-8")
        link = self.tmp / "link.json"
        link.symlink_to(victim)
        hard = self.tmp / "hard.json"
        os.link(victim, hard)
        for target in (link, hard):
            with self.subTest(target=target.name), contextlib.redirect_stdout(io.StringIO()):
                save_hierarchy(client, target)
                self.assertEqual(victim.read_text(encoding="utf-8"), "건드리면 안 됨")
                self.assertFalse(target.is_symlink())
                self.assertEqual(os.stat(target).st_nlink, 1)
                self.assertIn('"application"', target.read_text(encoding="utf-8"))

    def test_unexpected_error_writes_private_crash_log(self):
        """예상 밖 예외는 트레이스백을 작업 공간 logs/crash-*.log(0600)에 쓰고 그 경로만 안내하는지 본다."""
        path = self.write_app_config()
        with mock.patch("reproof.simple.cli.prepare_build", side_effect=RuntimeError("예상 밖 상태")):
            code, _, err, _, _ = self.run_cli(["build", "--config", str(path)], [])
        self.assertEqual(code, 2)
        self.assertIn("내부 오류입니다. 이 로그를 첨부해 알려 주세요: ", err)
        self.assertNotIn("Traceback", err)
        logs = list((self.tmp / "home" / "logs").glob("crash-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn(str(logs[0]), err)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        written = logs[0].read_text(encoding="utf-8")
        self.assertIn("Traceback", written)
        self.assertIn("예상 밖 상태", written)

    def test_unexpected_error_falls_back_to_stderr_when_crash_log_cannot_be_written(self):
        """crash 로그를 쓸 수 없으면(logs 자리에 파일) 원래 트레이스백을 stderr에 싣고 2를 돌려주는지 본다."""
        path = self.write_app_config()
        (self.tmp / "home").mkdir()
        (self.tmp / "home" / "logs").write_text("폴더 자리를 차지한 파일", encoding="utf-8")
        with mock.patch("reproof.simple.cli.prepare_build", side_effect=RuntimeError("예상 밖 상태")):
            code, _, err, _, _ = self.run_cli(["build", "--config", str(path)], [])
        self.assertEqual(code, 2)
        self.assertIn("내부 오류입니다", err)
        self.assertIn("RuntimeError: 예상 밖 상태", err)



if __name__ == "__main__":
    unittest.main()
