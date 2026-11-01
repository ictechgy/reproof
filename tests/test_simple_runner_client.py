"""실행기 Python 클라이언트·세션·빌드 캐시 테스트. loopback 가짜 실행기와 FakeToolRunner를 써서 Xcode 없이 실행된다."""
import contextlib
import json
import re
import signal
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from reproof.resources import ResourceError
from reproof.simple import runner_client
from reproof.simple.errors import SimpleError
from reproof.simple.hierarchy import INTERACTIVE_TYPES
from reproof.simple.runner_client import (
    RUNNER_FILES, RunnerClient, RunnerProducts, RunnerSession, build_runner, free_loopback_port,
)
from tests.simple_support import FakeToolRunner

BUNDLE = "io.reproof.example.mixedshop"
REPOSITORY = Path(__file__).resolve().parents[1]


def node(element_type, label="", children=()):
    """parse_hierarchy가 받는 노드 사전 하나를 만든다."""
    return {"type": element_type, "identifier": "", "label": label, "value": "", "placeholder": "",
            "frame": [0, 0, 100, 40], "enabled": True, "selected": False, "children": list(children)}


class _FakeRunnerHandler(BaseHTTPRequestHandler):
    """가짜 실행기 HTTP 처리기. 받은 요청을 server.requests에 쌓고 server.respond로 응답을 정한다."""

    def do_POST(self):
        """본문 JSON을 기록하고 (상태, 응답 사전)을 JSON으로 돌려준다."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        self.server.requests.append({"path": self.path, "body": body,
                                     "contentType": self.headers.get("Content-Type")})
        status, document = self.server.respond(body)
        raw = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        """요청마다 stderr에 찍히는 접근 로그를 끈다. 오류가 아니라 테스트 출력을 깨끗하게 두려는 것이다."""


def _status_or_ok(body):
    """status에는 대상 앱 정보를, 그 밖의 명령에는 ok만 돌려주는 기본 응답."""
    if body.get("action") == "status":
        return 200, {"ok": True, "targetBundle": BUNDLE, "targetState": "notRunning"}
    return 200, {"ok": True}


def _fake_process(pid=4321, *, exit_code=None):
    """Popen 대역. poll()은 exit_code(None이면 실행 중)를, 첫 wait(timeout)은 시간 초과를 낸다."""
    process = mock.Mock(pid=pid)
    process.poll.return_value = exit_code
    process.wait.side_effect = [subprocess.TimeoutExpired("xcodebuild", 2), 0]
    return process


@contextlib.contextmanager
def patched_process(process):
    """Popen·os.killpg·기본 환경을 바꿔 끼우고 (popen, killpg) 대역을 돌려준다.

    왜 build_default_environment도 바꾸나: subprocess.Popen 자체를 바꾸므로 xcode-select 조회가 대역을
    받으면 안 되고, DEVELOPER_DIR가 실제로 전달되는지도 확인하기 위해서다.
    """
    with mock.patch.object(runner_client.subprocess, "Popen", return_value=process) as popen, \
            mock.patch.object(runner_client.os, "killpg") as killpg, \
            mock.patch.object(runner_client, "build_default_environment",
                              return_value={"DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer"}):
        yield popen, killpg


class RunnerClientTests(unittest.TestCase):
    """RunnerClient·RunnerSession·build_runner의 동작을 확인한다."""

    def setUp(self):
        """임시 폴더를 만든다. 경로에 공백·한글을 넣어 argv가 깨지지 않는지도 함께 본다."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name) / "회사 앱"
        self.tmp.mkdir()

    def start_fake_runner(self, respond=_status_or_ok):
        """127.0.0.1 임의 포트에 가짜 실행기를 띄우고 서버를 돌려준다. 테스트가 끝나면 닫는다."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRunnerHandler)
        server.requests = []
        server.respond = respond
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def session(self, port, *, start_timeout=5.0):
        """테스트용 RunnerSession을 만든다."""
        products = RunnerProducts(self.tmp / "runner" / "SimpleRunner.xctestrun")
        return RunnerSession(products, udid="UDID-1", target_bundle=BUNDLE, port=port,
                             log_path=self.tmp / "logs" / "session.log", start_timeout=start_timeout)

    def test_command_posts_json_and_returns_payload(self):
        """POST /command에 action과 payload를 JSON으로 보내고 응답 사전을 그대로 돌려주는지 본다."""
        server = self.start_fake_runner()
        result = RunnerClient(server.server_address[1]).command("status")
        self.assertEqual(result, {"ok": True, "targetBundle": BUNDLE, "targetState": "notRunning"})
        RunnerClient(server.server_address[1]).tap_point(10.5, 20)
        self.assertEqual([request["body"] for request in server.requests],
                         [{"action": "status"}, {"action": "tap", "x": 10.5, "y": 20}])
        self.assertEqual({request["path"] for request in server.requests}, {"/command"})
        self.assertEqual({request["contentType"] for request in server.requests}, {"application/json"})

    def test_not_ok_response_raises_simple_error_with_runner_error(self):
        """ok가 false면 실행기의 error 문자열을 담은 runner_command_failed가 나는지 본다."""
        server = self.start_fake_runner(lambda body: (200, {"ok": False, "error": "target_not_running"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1]).terminate()
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("target_not_running", raised.exception.cause)
        self.assertIn("terminate", raised.exception.cause)

    def test_http_error_body_is_reported_as_runner_error(self):
        """실행기가 4xx로 JSON 오류를 보내면(urllib의 HTTPError) 그 error가 runner_command_failed에 담기는지 본다."""
        server = self.start_fake_runner(lambda body: (400, {"ok": False, "error": "invalid_json"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1]).command("status")
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("invalid_json", raised.exception.cause)

    def test_unreachable_runner_raises_simple_error(self):
        """아무도 듣지 않는 포트면 runner_unreachable이 나는지 본다."""
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(free_loopback_port(), timeout=2).command("status")
        self.assertEqual(raised.exception.code, "runner_unreachable")

    def test_hierarchy_parses_root(self):
        """hierarchy 응답의 root가 parse_hierarchy로 Element 트리가 되는지 본다."""
        tree = node("application", "MixedShop", [node("window", children=[node("button", "담기")])])
        server = self.start_fake_runner(lambda body: (200, {"ok": True, "root": tree}))
        root = RunnerClient(server.server_address[1]).hierarchy()
        self.assertEqual(root.element_type, "application")
        button = root.children[0].children[0]
        self.assertEqual((button.element_type, button.label, button.path), ("button", "담기", (0, 0)))
        self.assertEqual(server.requests[0]["body"], {"action": "hierarchy"})

    def test_session_passes_test_runner_prefixed_environment(self):
        """xcodebuild argv와 TEST_RUNNER_ 접두사 환경 변수, 새 세션 그룹, 로그 파일을 확인한다."""
        server = self.start_fake_runner()
        port = server.server_address[1]
        with patched_process(_fake_process()) as (popen, _):
            with self.session(port) as client:
                self.assertIsInstance(client, RunnerClient)
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["xcodebuild", "test-without-building", "-xctestrun",
                                str(self.tmp / "runner" / "SimpleRunner.xctestrun"), "-destination", "id=UDID-1",
                                "-only-testing:SimpleRunnerTests/RunnerSessionTests/testSession"])
        kwargs = popen.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(env["TEST_RUNNER_REPROOF_TARGET_BUNDLE"], "io.reproof.example.mixedshop")
        self.assertEqual(env["TEST_RUNNER_REPROOF_RUNNER_PORT"], str(port))
        self.assertEqual(env["DEVELOPER_DIR"], "/Applications/Xcode.app/Contents/Developer")
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue((self.tmp / "logs" / "session.log").exists())

    def test_session_exit_sends_stop_and_kills_process_group(self):
        """with를 빠져나갈 때 stop을 보내고 프로세스 그룹을 SIGTERM, 2초 뒤 SIGKILL로 정리하는지 본다."""
        server = self.start_fake_runner()
        process = _fake_process(pid=4321)
        with patched_process(process) as (_, killpg):
            with self.session(server.server_address[1]):
                self.assertEqual(killpg.call_args_list, [])
        self.assertEqual([request["body"]["action"] for request in server.requests], ["status", "stop"])
        self.assertEqual(killpg.call_args_list, [mock.call(4321, signal.SIGTERM), mock.call(4321, signal.SIGKILL)])
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call()])

    def test_session_start_failure_kills_process_group_and_points_to_log(self):
        """준비 전에 xcodebuild가 끝나면 그룹을 정리하고 로그 경로를 담은 runner_start_failed가 나는지 본다."""
        port = free_loopback_port()
        with patched_process(_fake_process(exit_code=65)) as (_, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(port):
                    self.fail("시작에 실패한 세션의 본문이 실행되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertEqual(raised.exception.log_path, self.tmp / "logs" / "session.log")
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)

    def test_session_rejects_runner_for_other_target_bundle(self):
        """같은 포트의 실행기가 다른 앱을 구동 중이면 연결하지 않고 runner_start_failed를 내는지 본다."""
        server = self.start_fake_runner(lambda body: (200, {"ok": True, "targetBundle": "com.other.app",
                                                            "targetState": "notRunning"}))
        with patched_process(_fake_process()) as (_, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(server.server_address[1], start_timeout=1.0):
                    self.fail("다른 앱을 구동하는 실행기에 연결되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)

    def test_type_text_rejects_more_than_256_characters(self):
        """256자를 넘는 입력은 실행기로 보내지 않고 바로 거절하는지 본다."""
        server = self.start_fake_runner()
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1]).type_text("가" * 257)
        self.assertEqual(raised.exception.code, "runner_text_too_long")
        self.assertEqual(server.requests, [])

    def test_runner_files_match_declared_distribution_resources(self):
        """RUNNER_FILES가 distribution-resources.json의 ios/SimpleRunner 항목과 정확히 같은지 본다."""
        declared = json.loads((REPOSITORY / "distribution-resources.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(list(RUNNER_FILES), [name for name in declared if name.startswith("ios/SimpleRunner/")])
        self.assertIn("ios/SimpleRunner/SimpleRunner.xcodeproj/project.pbxproj", RUNNER_FILES)

    def test_swift_type_names_cover_python_interactive_types(self):
        """Swift HierarchyExport의 type 표가 case 이름 그대로이고 INTERACTIVE_TYPES를 모두 내보내는지 본다.

        왜: 한쪽만 이름을 바꾸면 Python이 버튼 등을 interactive로 보지 못해 엉뚱한 요소를 녹화하게 된다.
        """
        source = (REPOSITORY / "ios/SimpleRunner/RunnerTests/HierarchyExport.swift").read_text(encoding="utf-8")
        pairs = re.findall(r'\.(\w+): "(\w+)"', source)
        self.assertGreater(len(pairs), 80)
        self.assertEqual([case for case, name in pairs if case != name], [])
        self.assertLessEqual(INTERACTIVE_TYPES, {name for _, name in pairs})

    def test_build_runner_copies_sources_builds_once_and_reuses_cache(self):
        """소스 복사, 고정 argv, 캐시 디렉터리 이름(16자), 두 번째 호출의 재사용을 확인한다(경로에 공백·한글 포함)."""
        builds = []

        def build(argv, cwd, env_extra):
            builds.append((argv, cwd))
            products = Path(argv[argv.index("-derivedDataPath") + 1]) / "Build" / "Products"
            (products / "Debug-iphonesimulator" / "SimpleRunnerTests-Runner.app").mkdir(parents=True)
            (products / "SimpleRunner_iphonesimulator27.0-arm64.xctestrun").write_text("plist")
            return ""

        runner = FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.0\nBuild version 27A266a\n"),
                                 (("xcodebuild", "build-for-testing"), build)])
        output = self.tmp / "출력 폴더"
        first = build_runner(output, runner)
        second = build_runner(output, runner)
        self.assertEqual(first, second)
        self.assertEqual(len(builds), 1)
        argv, cwd = builds[0]
        self.assertEqual(argv, ["xcodebuild", "build-for-testing", "-project", "SimpleRunner.xcodeproj",
                                "-scheme", "SimpleRunner", "-sdk", "iphonesimulator",
                                "-destination", "generic/platform=iOS Simulator",
                                "-derivedDataPath", str(output / "runner-dd"), "CODE_SIGNING_ALLOWED=NO"])
        self.assertEqual(cwd, output / "runner-src")
        self.assertTrue((output / "runner-src" / "SimpleRunner.xcodeproj" / "project.pbxproj").is_file())
        key_dir = first.xctestrun.parent
        self.assertEqual(key_dir.parent, output / "runner")
        self.assertRegex(key_dir.name, re.compile(r"[0-9a-f]{16}\Z"))
        self.assertEqual(first.xctestrun.name, "SimpleRunner_iphonesimulator27.0-arm64.xctestrun")
        self.assertTrue((key_dir / "Debug-iphonesimulator" / "SimpleRunnerTests-Runner.app").is_dir())

    def test_build_runner_cache_key_changes_with_xcode_version(self):
        """Xcode 버전이 바뀌면 캐시 키가 달라져 다시 빌드하는지 본다."""
        def build(argv, cwd, env_extra):
            products = Path(argv[argv.index("-derivedDataPath") + 1]) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "SimpleRunner.xctestrun").write_text("plist")
            return ""

        output = self.tmp / "out"
        older = build_runner(output, FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.0\n"),
                                                     (("xcodebuild", "build-for-testing"), build)]))
        newer = build_runner(output, FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.1\n"),
                                                     (("xcodebuild", "build-for-testing"), build)]))
        self.assertNotEqual(older.xctestrun.parent, newer.xctestrun.parent)

    def test_build_runner_without_runner_source_raises_simple_error(self):
        """실행기 소스 리소스를 읽지 못하면 xcodebuild를 부르기 전에 runner_source_missing을 내는지 본다."""
        runner = FakeToolRunner([])
        with mock.patch.object(runner_client, "read_resource", side_effect=ResourceError()):
            with self.assertRaises(SimpleError) as raised:
                build_runner(self.tmp / "out", runner)
        self.assertEqual(raised.exception.code, "runner_source_missing")
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
