"""실행기 Python 클라이언트·세션·빌드 캐시 테스트. loopback 가짜 실행기와 FakeToolRunner를 써서 Xcode 없이 실행된다."""
import contextlib
import hashlib
import hmac
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
TOKEN = "test-session-token"
REPOSITORY = Path(__file__).resolve().parents[1]
BEARER = "Bearer "
TOKEN_ENV = "TEST_RUNNER_REPROOF_RUNNER_TOKEN"
PROOF_KEY_ENV = "TEST_RUNNER_REPROOF_RUNNER_PROOF_KEY"
# 세션 없이 RunnerClient만 쓰는 테스트에서 가짜 실행기가 쓰는 proof 키.
CLIENT_ONLY_PROOF_KEY = "client-only-proof-key"


def node(element_type, label="", children=()):
    """parse_hierarchy가 받는 노드 사전 하나를 만든다."""
    return {"type": element_type, "identifier": "", "label": label, "value": "", "placeholder": "",
            "frame": [0, 0, 100, 40], "enabled": True, "selected": False, "children": list(children)}


def expected_proof(key, bundle):
    """명세 그대로 계산한 status proof: hex(HMAC-SHA256(key=proof 키, msg="reproof-runner-status:" + bundle)).

    왜 테스트에서 따로 계산하나: 구현 함수를 그대로 쓰면 구현이 틀려도 테스트가 함께 틀려 통과하기 때문이다.
    """
    message = ("reproof-runner-status:" + bundle).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def status_document(key, bundle=BUNDLE, **overrides):
    """정상 실행기의 status 응답(key로 proof를 만든다). overrides로 proof 등을 바꿔 가짜 실행기를 흉내 낸다."""
    document = {"ok": True, "targetBundle": bundle, "targetState": "notRunning",
                "proof": expected_proof(key, bundle)}
    document.update(overrides)
    return document


def proof_key_of(env):
    """세션 환경에서 proof 키를 꺼낸다. 세션 없이 클라이언트만 쓰는 테스트면 고정 키를 쓴다."""
    return env[PROOF_KEY_ENV] if env else CLIENT_ONLY_PROOF_KEY


class _FakeRunnerHandler(BaseHTTPRequestHandler):
    """가짜 실행기 HTTP 처리기. 받은 요청을 server.requests에 쌓고 server.respond(본문, 토큰, 세션 환경)로 응답을 정한다.

    세션 환경은 실제 실행기가 xcodebuild에서 받는 환경 변수(proof 키 등)를 흉내 내려고 넘긴다.
    """

    def do_POST(self):
        """본문 JSON과 헤더를 기록하고 (상태, 응답 사전)을 JSON으로 돌려준다."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        authorization = self.headers.get("Authorization")
        self.server.requests.append({"path": self.path, "body": body, "authorization": authorization,
                                     "contentType": self.headers.get("Content-Type"),
                                     "host": self.headers.get("Host")})
        token = authorization[len(BEARER):] if authorization and authorization.startswith(BEARER) else None
        status, document = self.server.respond(body, token, self.server.session_env())
        raw = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        """요청마다 stderr에 찍히는 접근 로그를 끈다. 오류가 아니라 테스트 출력을 깨끗하게 두려는 것이다."""


def _honest_runner(body, token, env):
    """정상 실행기 흉내: status에는 세션 proof 키로 만든 proof를, 그 밖의 명령에는 ok만 돌려준다."""
    if body.get("action") == "status":
        return 200, status_document(proof_key_of(env))
    return 200, {"ok": True}


def _token_checking_runner(expected_token):
    """expected_token이 아닌 요청(토큰 없음 포함)은 실제 실행기처럼 401 unauthorized로 거절하는 응답기."""
    def respond(body, token, env):
        if token != expected_token:
            return 401, {"ok": False, "error": "unauthorized"}
        return _honest_runner(body, token, env)
    return respond


def _fake_process(pid=4321, *, exit_code=None):
    """Popen 대역. poll()은 exit_code(None이면 실행 중)를, 첫 wait(timeout)은 시간 초과를 낸다."""
    process = mock.Mock(pid=pid)
    process.poll.return_value = exit_code
    process.wait.side_effect = [subprocess.TimeoutExpired("xcodebuild", 2), 0]
    return process


@contextlib.contextmanager
def patched_process(process, server=None):
    """Popen·os.killpg·기본 환경을 바꿔 끼우고 (popen, killpg) 대역을 돌려준다.

    server를 주면 가짜 실행기가 Popen에 넘어간 환경(proof 키)을 볼 수 있게 이어 준다. 실제 실행기가
    xcodebuild로부터 환경 변수를 받는 것을 흉내 낸다.
    왜 build_default_environment도 바꾸나: subprocess.Popen 자체를 바꾸므로 xcode-select 조회가 대역을
    받으면 안 되고, DEVELOPER_DIR가 실제로 전달되는지도 확인하기 위해서다.
    """
    with mock.patch.object(runner_client.subprocess, "Popen", return_value=process) as popen, \
            mock.patch.object(runner_client.os, "killpg") as killpg, \
            mock.patch.object(runner_client, "build_default_environment",
                              return_value={"DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer"}):
        if server is not None:
            server.session_env = lambda: popen.call_args.kwargs["env"] if popen.call_args else None
        yield popen, killpg


def session_env(popen):
    """patch된 Popen에 넘어간 환경을 돌려준다."""
    return popen.call_args.kwargs["env"]


def session_token(popen):
    """patch된 Popen에 넘어간 환경에서 세션 토큰을 꺼낸다."""
    return session_env(popen)[TOKEN_ENV]


class RunnerClientTests(unittest.TestCase):
    """RunnerClient·RunnerSession·build_runner의 동작을 확인한다."""

    def setUp(self):
        """임시 폴더를 만든다. 경로에 공백·한글을 넣어 argv가 깨지지 않는지도 함께 본다."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name) / "회사 앱"
        self.tmp.mkdir()
        self.log_path = self.tmp / "logs" / "session.log"

    def start_fake_runner(self, respond=_honest_runner):
        """127.0.0.1 임의 포트에 가짜 실행기를 띄우고 서버를 돌려준다. 테스트가 끝나면 닫는다."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRunnerHandler)
        server.requests = []
        server.respond = respond
        server.session_env = lambda: None
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
                             log_path=self.log_path, start_timeout=start_timeout)

    def assert_start_failed(self, server, *, start_timeout=1.0):
        """세션 시작이 runner_start_failed로 끝나고 본문이 실행되지 않으며 그룹이 정리되는지 확인한다.

        반환: (오류, 세션 환경). 비밀 값이 사용자 안내문에 섞이지 않았는지 호출자가 확인할 수 있게 한다.
        """
        with patched_process(_fake_process(), server) as (popen, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(server.server_address[1], start_timeout=start_timeout):
                    self.fail("검증에 실패한 실행기에 연결되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)
        return raised.exception, session_env(popen)

    def test_command_posts_json_and_returns_payload(self):
        """POST /command에 action·payload JSON과 Bearer 토큰·Host를 보내고 응답 사전을 그대로 돌려주는지 본다."""
        server = self.start_fake_runner()
        port = server.server_address[1]
        result = RunnerClient(port, token=TOKEN).command("status")
        self.assertEqual(result, status_document(CLIENT_ONLY_PROOF_KEY))
        RunnerClient(port, token=TOKEN).tap_point(10.5, 20)
        self.assertEqual([request["body"] for request in server.requests],
                         [{"action": "status"}, {"action": "tap", "x": 10.5, "y": 20}])
        self.assertEqual({request["path"] for request in server.requests}, {"/command"})
        self.assertEqual({request["contentType"] for request in server.requests}, {"application/json"})
        self.assertEqual({request["authorization"] for request in server.requests}, {BEARER + TOKEN})
        self.assertEqual({request["host"] for request in server.requests}, {f"127.0.0.1:{port}"})

    def test_client_requires_token(self):
        """토큰 없이는 RunnerClient를 만들 수 없는지 본다(인증 없는 요청을 아예 보낼 수 없게)."""
        with self.assertRaises(TypeError):
            RunnerClient(1234)

    def test_runner_rejecting_missing_or_wrong_token_raises_unauthorized(self):
        """실행기가 토큰을 401로 거절하면 unauthorized를 담은 runner_command_failed가 나고, 맞는 토큰은 통과하는지 본다."""
        server = self.start_fake_runner(_token_checking_runner("right-token"))
        port = server.server_address[1]
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(port, token="wrong-token").command("status")
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("unauthorized", raised.exception.cause)
        self.assertNotIn("wrong-token", raised.exception.user_message())
        self.assertTrue(RunnerClient(port, token="right-token").command("status")["ok"])

    def test_not_ok_response_raises_simple_error_with_runner_error(self):
        """ok가 false면 실행기의 error 문자열을 담은 runner_command_failed가 나는지 본다."""
        server = self.start_fake_runner(lambda body, token, env: (200, {"ok": False, "error": "target_not_running"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).terminate()
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("target_not_running", raised.exception.cause)
        self.assertIn("terminate", raised.exception.cause)

    def test_http_error_body_is_reported_as_runner_error(self):
        """실행기가 4xx로 JSON 오류를 보내면(urllib의 HTTPError) 그 error가 runner_command_failed에 담기는지 본다."""
        server = self.start_fake_runner(lambda body, token, env: (400, {"ok": False, "error": "invalid_json"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).command("status")
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("invalid_json", raised.exception.cause)

    def test_unreachable_runner_raises_simple_error(self):
        """아무도 듣지 않는 포트면 runner_unreachable이 나는지 본다."""
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(free_loopback_port(), token=TOKEN, timeout=2).command("status")
        self.assertEqual(raised.exception.code, "runner_unreachable")

    def test_hierarchy_parses_root(self):
        """hierarchy 응답의 root가 parse_hierarchy로 Element 트리가 되는지 본다."""
        tree = node("application", "MixedShop", [node("window", children=[node("button", "담기")])])
        server = self.start_fake_runner(lambda body, token, env: (200, {"ok": True, "root": tree}))
        root = RunnerClient(server.server_address[1], token=TOKEN).hierarchy()
        self.assertEqual(root.element_type, "application")
        button = root.children[0].children[0]
        self.assertEqual((button.element_type, button.label, button.path), ("button", "담기", (0, 0)))
        self.assertEqual(server.requests[0]["body"], {"action": "hierarchy"})

    def test_session_passes_test_runner_prefixed_environment(self):
        """xcodebuild argv, TEST_RUNNER_ 환경 변수(포트·대상 앱·토큰·proof 키), 새 세션 그룹, 토큰을 쓰는 클라이언트를 확인한다."""
        server = self.start_fake_runner()
        port = server.server_address[1]
        with patched_process(_fake_process(), server) as (popen, _):
            with self.session(port) as client:
                client.terminate()
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["xcodebuild", "test-without-building", "-xctestrun",
                                str(self.tmp / "runner" / "SimpleRunner.xctestrun"), "-destination", "id=UDID-1",
                                "-only-testing:SimpleRunnerTests/RunnerSessionTests/testSession"])
        kwargs = popen.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(env["TEST_RUNNER_REPROOF_TARGET_BUNDLE"], "io.reproof.example.mixedshop")
        self.assertEqual(env["TEST_RUNNER_REPROOF_RUNNER_PORT"], str(port))
        self.assertEqual(env["DEVELOPER_DIR"], "/Applications/Xcode.app/Contents/Developer")
        token, proof_key = env[TOKEN_ENV], env[PROOF_KEY_ENV]
        self.assertGreaterEqual(len(token), 43)  # secrets.token_urlsafe(32)는 43자 이상이다.
        self.assertGreaterEqual(len(proof_key), 43)
        self.assertNotEqual(token, proof_key)
        self.assertEqual([request["body"]["action"] for request in server.requests], ["status", "terminate", "stop"])
        self.assertEqual({request["authorization"] for request in server.requests}, {BEARER + token})
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(self.log_path.exists())

    def test_each_session_uses_new_secrets(self):
        """세션마다 토큰과 proof 키를 새로 만드는지 본다. 이전 세션 값으로 새 세션을 조작·사칭하지 못하게 하려는 것이다."""
        server = self.start_fake_runner()
        secrets_per_session = []
        for _ in range(2):
            with patched_process(_fake_process(), server) as (popen, _):
                with self.session(server.server_address[1]):
                    secrets_per_session.append((session_env(popen)[TOKEN_ENV], session_env(popen)[PROOF_KEY_ENV]))
        self.assertNotEqual(secrets_per_session[0][0], secrets_per_session[1][0])
        self.assertNotEqual(secrets_per_session[0][1], secrets_per_session[1][1])

    def test_proof_key_never_leaves_the_process_environment(self):
        """proof 키는 xcodebuild 환경으로만 넘기고 HTTP 요청(헤더·본문)에는 절대 싣지 않는지 본다.

        왜: 요청에 실리는 Bearer 토큰은 포트를 가로챈 프로세스도 받는다. proof 키까지 실리면 그 프로세스가
        proof를 만들어 진짜 실행기 행세를 할 수 있다.
        """
        server = self.start_fake_runner()
        with patched_process(_fake_process(), server) as (popen, _):
            with self.session(server.server_address[1]) as client:
                client.tap_point(1, 2)
        proof_key = session_env(popen)[PROOF_KEY_ENV]
        self.assertEqual(len(server.requests), 3)
        for request in server.requests:
            self.assertNotIn(proof_key, json.dumps(request))

    def test_session_exit_sends_stop_and_kills_process_group(self):
        """with를 빠져나갈 때 stop을 보내고 프로세스 그룹을 SIGTERM, 2초 뒤 SIGKILL로 정리하는지 본다."""
        server = self.start_fake_runner()
        process = _fake_process(pid=4321)
        with patched_process(process, server) as (_, killpg):
            with self.session(server.server_address[1]):
                self.assertEqual(killpg.call_args_list, [])
        self.assertEqual([request["body"]["action"] for request in server.requests], ["status", "stop"])
        self.assertEqual(killpg.call_args_list, [mock.call(4321, signal.SIGTERM), mock.call(4321, signal.SIGKILL)])
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call()])

    def test_session_log_never_contains_secrets(self):
        """Python이 세션 로그에 쓰는 진단(stop 실패, SIGKILL 정리)에 토큰·proof 키가 들어가지 않는지 본다."""
        def reject_stop(body, token, env):
            if body.get("action") == "stop":
                return 401, {"ok": False, "error": "unauthorized"}
            return _honest_runner(body, token, env)

        server = self.start_fake_runner(reject_stop)
        with patched_process(_fake_process(), server) as (popen, _):
            with self.session(server.server_address[1]):
                env = session_env(popen)
        written = self.log_path.read_text(encoding="utf-8")
        self.assertEqual(written.count("[reproof]"), 2)  # 진단이 실제로 쓰였는지 확인해 빈 로그로 통과하지 않게 한다.
        self.assertNotIn(env[TOKEN_ENV], written)
        self.assertNotIn(env[PROOF_KEY_ENV], written)

    def test_session_rejects_wrong_or_missing_status_proof(self):
        """status proof가 틀리거나 없거나 문자열이 아니면 포트를 가로챈 가짜 실행기로 보고 runner_start_failed를 내는지 본다.

        "leaked-bearer-token": 가짜 실행기가 요청에서 받은 Bearer 토큰으로 proof를 만들어도 통과하지 못해야 한다.
        """
        impostors = {
            "leaked-bearer-token": lambda token, env: status_document(token),
            "unrelated-key": lambda token, env: status_document("guessed-key"),
            "missing": lambda token, env: {k: v for k, v in status_document(proof_key_of(env)).items() if k != "proof"},
            "not-string": lambda token, env: status_document(proof_key_of(env), proof=123),
            "other-message": lambda token, env: status_document(
                proof_key_of(env), proof=expected_proof(proof_key_of(env), "com.other.app")),
        }
        for label, make_status in impostors.items():
            with self.subTest(label=label):
                server = self.start_fake_runner(
                    lambda body, token, env, make_status=make_status:
                        (200, make_status(token or "", env)) if body.get("action") == "status" else (200, {"ok": True}))
                error, env = self.assert_start_failed(server)
                self.assertNotIn(env[TOKEN_ENV], error.user_message())
                self.assertNotIn(env[PROOF_KEY_ENV], error.user_message())
                self.assertEqual([request["body"]["action"] for request in server.requests], ["status"])

    def test_session_rejects_runner_for_other_target_bundle(self):
        """proof가 맞아도 다른 앱을 구동하는 실행기면 연결하지 않고 runner_start_failed를 내는지 본다."""
        server = self.start_fake_runner(
            lambda body, token, env: (200, status_document(proof_key_of(env), "com.other.app")))
        self.assert_start_failed(server)

    def test_session_start_failure_kills_process_group_and_points_to_log(self):
        """준비 전에 xcodebuild가 끝나면 그룹을 정리하고 로그 경로를 담은 runner_start_failed가 나는지 본다."""
        port = free_loopback_port()
        with patched_process(_fake_process(exit_code=65)) as (_, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(port):
                    self.fail("시작에 실패한 세션의 본문이 실행되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertEqual(raised.exception.log_path, self.log_path)
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)

    def test_type_text_rejects_more_than_256_characters(self):
        """256자를 넘는 입력은 실행기로 보내지 않고 바로 거절하는지 본다."""
        server = self.start_fake_runner()
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).type_text("가" * 257)
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
