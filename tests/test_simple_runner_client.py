"""실행기 Python 클라이언트·세션·빌드 캐시 테스트. loopback 가짜 실행기와 FakeToolRunner를 써서 Xcode 없이 실행된다."""
import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import signal
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from reproof import repair
from reproof.resources import ResourceError
from reproof.simple import runner_client
from reproof.simple.errors import SimpleError
from reproof.simple.hierarchy import INTERACTIVE_TYPES
from reproof.simple.runner_client import (
    RUNNER_FILES, RunnerClient, RunnerProducts, RunnerSession, build_runner, free_loopback_port,
)
from tests.simple_support import FakeToolRunner, wait_until_zombie

BUNDLE = "io.reproof.example.mixedshop"
TOKEN = "test-session-token"
REPOSITORY = Path(__file__).resolve().parents[1]
BEARER = "Bearer "
TOKEN_ENV = "TEST_RUNNER_REPROOF_RUNNER_TOKEN"
PROOF_KEY_ENV = "TEST_RUNNER_REPROOF_RUNNER_PROOF_KEY"
# 세션 없이 RunnerClient만 쓰는 테스트에서 가짜 실행기가 쓰는 proof 키.
CLIENT_ONLY_PROOF_KEY = "client-only-proof-key"
NONCE = "ab" * 32
NONCE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def node(element_type, label="", children=()):
    """parse_hierarchy가 받는 노드 사전 하나를 만든다."""
    return {"type": element_type, "identifier": "", "label": label, "value": "", "placeholder": "",
            "frame": [0, 0, 100, 40], "enabled": True, "selected": False, "children": list(children)}


def expected_proof(key, bundle, nonce):
    """명세 그대로 계산한 proof: hex(HMAC-SHA256(key, "reproof-runner-status:" + bundle + ":" + nonce)).

    왜 테스트에서 따로 계산하나: 구현 함수를 그대로 쓰면 구현이 틀려도 테스트가 함께 틀려 통과하기 때문이다.
    """
    message = f"reproof-runner-status:{bundle}:{nonce}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def status_document(key, nonce, bundle=BUNDLE, **overrides):
    """정상 실행기의 status 응답(key·nonce로 proof를 만든다). overrides로 값을 바꿔 가짜 실행기를 흉내 낸다."""
    document = {"ok": True, "targetBundle": bundle, "targetState": "notRunning",
                "proof": expected_proof(key, bundle, nonce)}
    document.update(overrides)
    return document


def proof_key_of(env):
    """세션 환경에서 proof 키를 꺼낸다. 세션 없이 클라이언트만 쓰는 테스트면 고정 키를 쓴다."""
    return env[PROOF_KEY_ENV] if env else CLIENT_ONLY_PROOF_KEY


class _FakeRunnerHandler(BaseHTTPRequestHandler):
    """가짜 실행기 HTTP 처리기. 받은 요청(헤더 전체 포함)을 server.requests에 쌓고 server.respond로 응답을 정한다.

    respond(본문, Bearer 토큰 또는 None, 세션 환경). 세션 환경은 실제 실행기가 xcodebuild에서 받는
    환경 변수(토큰·proof 키)를 흉내 내려고 넘긴다. server.delay초만큼 늦게 답할 수 있다.
    """

    def do_POST(self):
        """본문 JSON과 헤더를 기록하고 (상태, 응답 사전)을 JSON으로 돌려준다."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        authorization = self.headers.get("Authorization")
        self.server.requests.append({"path": self.path, "body": body, "authorization": authorization,
                                     "headers": dict(self.headers.items())})
        token = authorization[len(BEARER):] if authorization and authorization.startswith(BEARER) else None
        status, document = self.server.respond(body, token, self.server.session_env())
        time.sleep(self.server.delay)
        raw = json.dumps(document).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            # 시간 초과·연결 끊김 테스트에서는 클라이언트가 먼저 떠나는 것이 예상된 결과다. 트레이스백으로 출력을 어지럽히지
            # 않고, 응답을 보내지 못한 사실은 기록해 테스트가 필요하면 확인할 수 있게 한다.
            self.server.requests[-1]["is_reply_lost"] = True

    def log_message(self, format, *args):
        """요청마다 stderr에 찍히는 접근 로그를 끈다. 오류가 아니라 테스트 출력을 깨끗하게 두려는 것이다."""


def _honest_runner(body, token, env):
    """정상 실행기 흉내. status(nonce)는 토큰 없이 proof로 답하고, 그 밖의 명령은 세션 토큰이 맞아야 ok다."""
    if body.get("action") == "status":
        return 200, status_document(proof_key_of(env), body.get("nonce", ""))
    if token is None or (env is not None and token != env[TOKEN_ENV]):
        return 401, {"ok": False, "error": "unauthorized"}
    return 200, {"ok": True}


def _token_checking_runner(expected_token):
    """expected_token이 아닌 명령 요청(토큰 없음 포함)을 실제 실행기처럼 401 unauthorized로 거절하는 응답기."""
    def respond(body, token, env):
        if token != expected_token:
            return 401, {"ok": False, "error": "unauthorized"}
        return 200, {"ok": True}
    return respond


def _impostor(make_status):
    """포트를 가로챈 가짜 실행기. status에는 make_status(본문, 세션 환경)를, 나머지에는 ok를 돌려준다."""
    def respond(body, token, env):
        if body.get("action") == "status":
            return 200, make_status(body, env)
        return 200, {"ok": True}
    return respond


class _HostileStatusLineHandler(socketserver.BaseRequestHandler):
    """HTTP 대신 ESC·제어 문자가 섞인 긴 상태 줄을 돌려주는 가짜 상대(http.client BadStatusLine을 일으킨다)."""

    def handle(self):
        """요청을 읽고 적대적인 상태 줄을 보낸 뒤 닫는다. 클라이언트가 먼저 끊으면(예상된 경우) 조용히 끝낸다."""
        try:
            self.request.recv(65536)
            self.request.sendall(b"\x1b[31mEVIL\x07" + b"A" * 5000 + b"\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return  # 클라이언트가 상태 줄을 다 받기 전에 끊는 것은 이 테스트가 확인하려는 동작의 일부다.


def _fake_process(pid=4321, *, exit_code=None, exits_after_stop=False):
    """Popen 대역. poll()은 exit_code(None이면 실행 중)를 돌려준다.

    exits_after_stop이 True면 timeout이 있는 wait도 바로 끝나고(stop 뒤 스스로 종료), False면 시간 초과를 낸다.
    SIGTERM 유예 대기(2초) 뒤의 wait와 timeout 없는 wait()는 바로 끝난다(SIGKILL 뒤 회수를 흉내 낸다).
    """
    process = mock.Mock(pid=pid)
    process.poll.return_value = exit_code
    state = {"is_terminated": False}

    def wait(timeout=None):
        if timeout is None or exits_after_stop or state["is_terminated"]:
            return 0
        if timeout == runner_client._TERMINATE_GRACE_SECONDS:
            state["is_terminated"] = True  # 다음 wait는 SIGKILL 뒤 회수다.
        raise subprocess.TimeoutExpired("xcodebuild", timeout)

    process.wait.side_effect = wait
    return process


@contextlib.contextmanager
def patched_process(process, server=None):
    """Popen·os.killpg·기본 환경을 바꿔 끼우고 (popen, killpg) 대역을 돌려준다.

    server를 주면 가짜 실행기가 Popen에 넘어간 환경(토큰·proof 키)을 볼 수 있게 이어 준다. 실제 실행기가
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


def session_dir_of(popen):
    """patch된 Popen argv의 -resultBundlePath에서 세션 폴더를 꺼낸다."""
    argv = popen.call_args.args[0]
    return Path(argv[argv.index("-resultBundlePath") + 1]).parent


def secrets_of(popen):
    """세션 토큰과 proof 키를 돌려준다."""
    env = session_env(popen)
    return env[TOKEN_ENV], env[PROOF_KEY_ENV]


class RunnerClientTests(unittest.TestCase):
    """RunnerClient·RunnerSession·build_runner의 동작을 확인한다."""

    def setUp(self):
        """임시 폴더를 만든다. 경로에 공백·한글을 넣어 argv가 깨지지 않는지도 함께 본다."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name) / "회사 앱"
        self.tmp.mkdir()
        self.log_path = self.tmp / "logs" / "session.log"

    def start_fake_runner(self, respond=_honest_runner, *, delay=0.0):
        """127.0.0.1 임의 포트에 가짜 실행기를 띄우고 서버를 돌려준다. 테스트가 끝나면 닫는다."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRunnerHandler)
        server.requests = []
        server.respond = respond
        server.delay = delay
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

        반환: (오류, popen 대역). 비밀 값이 안내문·요청에 섞이지 않았는지 호출자가 확인할 수 있게 한다.
        """
        with patched_process(_fake_process(), server) as (popen, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(server.server_address[1], start_timeout=start_timeout):
                    self.fail("검증에 실패한 실행기에 연결되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)
        return raised.exception, popen

    # --- RunnerClient ---

    def start_hostile_status_line_server(self):
        """제어 문자가 섞인 긴 상태 줄로 답하는 TCP 서버를 127.0.0.1 임의 포트에 띄우고 포트를 돌려준다."""
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HostileStatusLineHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def assert_safe_text(self, text, *, limit):
        """text에 제어 문자(ESC 포함)가 없고 길이가 limit 이하인지 확인한다."""
        self.assertFalse(any(ord(character) < 32 or ord(character) == 127 for character in text), repr(text[:80]))
        self.assertLessEqual(len(text), limit)

    def test_command_posts_json_and_returns_payload(self):
        """POST /command에 action·payload JSON과 Bearer 토큰·Host·Content-Type을 보내고 응답 사전을 돌려주는지 본다."""
        server = self.start_fake_runner(_token_checking_runner(TOKEN))
        port = server.server_address[1]
        self.assertEqual(RunnerClient(port, token=TOKEN).command("tap", x=10.5, y=20), {"ok": True})
        request = server.requests[0]
        self.assertEqual((request["path"], request["body"]), ("/command", {"action": "tap", "x": 10.5, "y": 20}))
        self.assertEqual(request["authorization"], BEARER + TOKEN)
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["headers"]["Host"], f"127.0.0.1:{port}")

    def test_handshake_sends_nonce_without_token(self):
        """handshake는 토큰 없이 status와 nonce만 보내는지 본다. 검증 전 상대에게 토큰을 넘기지 않기 위해서다."""
        server = self.start_fake_runner()
        result = RunnerClient(server.server_address[1], token=TOKEN).handshake(NONCE)
        self.assertEqual(result, status_document(CLIENT_ONLY_PROOF_KEY, NONCE))
        request = server.requests[0]
        self.assertEqual(request["body"], {"action": "status", "nonce": NONCE})
        self.assertNotIn("Authorization", request["headers"])
        self.assertNotIn(TOKEN, json.dumps(request))

    def test_client_requires_token(self):
        """토큰 없이는 RunnerClient를 만들 수 없는지 본다(인증 없는 명령을 아예 보낼 수 없게)."""
        with self.assertRaises(TypeError):
            RunnerClient(1234)

    def test_runner_rejecting_missing_or_wrong_token_raises_unauthorized(self):
        """실행기가 토큰을 401로 거절하면 unauthorized를 담은 runner_command_failed가 나고, 맞는 토큰은 통과하는지 본다."""
        server = self.start_fake_runner(_token_checking_runner("right-token"))
        port = server.server_address[1]
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(port, token="wrong-token").command("tap", x=1, y=1)
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("unauthorized", raised.exception.cause)
        self.assertNotIn("wrong-token", raised.exception.user_message())
        self.assertTrue(RunnerClient(port, token="right-token").command("tap", x=1, y=1)["ok"])

    def test_not_ok_response_raises_simple_error_with_runner_error(self):
        """ok가 false면 실행기의 error 문자열을 담은 runner_command_failed가 나는지 본다."""
        server = self.start_fake_runner(lambda body, token, env: (200, {"ok": False, "error": "target_not_running"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).terminate()
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("target_not_running", raised.exception.cause)
        self.assertIn("terminate", raised.exception.cause)

    def test_runner_error_text_is_clamped_and_sanitized(self):
        """실행기가 보낸 error는 제어 문자를 없애고 120자로 자른 뒤에만 안내문에 넣는지 본다(터미널 조작·도배 방지)."""
        hostile = "\x1b[31m빨강\nline\r\x07" + "가" * 500
        server = self.start_fake_runner(lambda body, token, env: (200, {"ok": False, "error": hostile}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).terminate()
        cause = raised.exception.cause
        self.assertFalse(any(ord(character) < 32 or ord(character) == 127 for character in cause))
        shown = cause.split("처리하지 못했습니다: ", 1)[1]
        self.assertLessEqual(len(shown), 121)
        self.assertIn("빨강", shown)

    def test_http_error_body_is_reported_as_runner_error(self):
        """실행기가 4xx로 JSON 오류를 보내면(urllib의 HTTPError) 그 error가 runner_command_failed에 담기는지 본다."""
        server = self.start_fake_runner(lambda body, token, env: (400, {"ok": False, "error": "invalid_json"}))
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).command("tap", x=1, y=1)
        self.assertEqual(raised.exception.code, "runner_command_failed")
        self.assertIn("invalid_json", raised.exception.cause)

    def test_unreachable_runner_raises_simple_error(self):
        """아무도 듣지 않는 포트면 runner_unreachable이 나는지 본다."""
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(free_loopback_port(), token=TOKEN, timeout=2).command("tap", x=1, y=1)
        self.assertEqual(raised.exception.code, "runner_unreachable")

    def test_unreachable_error_from_hostile_peer_is_sanitized(self):
        """상대가 제어 문자가 섞인 긴 상태 줄을 보내도(BadStatusLine) runner_unreachable 안내문에는 다듬은 짧은 글만 들어가는지 본다."""
        port = self.start_hostile_status_line_server()
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(port, token=TOKEN, timeout=5).command("tap", x=1, y=1)
        self.assertEqual(raised.exception.code, "runner_unreachable")
        self.assert_safe_text(raised.exception.cause, limit=300)
        self.assertIn("EVIL", raised.exception.cause)

    def test_start_failure_cause_from_hostile_peer_is_sanitized(self):
        """시작 handshake 상대가 적대적인 상태 줄을 보내도 runner_start_failed 원인에 제어 문자·긴 글이 실리지 않는지 본다."""
        port = self.start_hostile_status_line_server()
        with patched_process(_fake_process()):
            with self.assertRaises(SimpleError) as raised:
                with self.session(port, start_timeout=1.0):
                    self.fail("가짜 상대에 연결되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assert_safe_text(raised.exception.cause, limit=500)

    def test_long_gesture_timeout_covers_requested_duration(self):
        """long_press·swipe는 기본 제한 시간보다 길어도 duration+10초까지 기다리고, 다른 명령은 기본 시간을 지키는지 본다."""
        server = self.start_fake_runner(_token_checking_runner(TOKEN), delay=1.5)
        client = RunnerClient(server.server_address[1], token=TOKEN, timeout=1.0)
        self.assertTrue(client.command("long_press", x=1, y=1, duration=1.0)["ok"])
        self.assertTrue(client.command("swipe", fromX=1, fromY=1, toX=2, toY=2, duration=1.0)["ok"])
        with self.assertRaises(SimpleError) as raised:
            client.command("tap", x=1, y=1)
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

    def test_type_text_rejects_more_than_256_characters(self):
        """256자를 넘는 입력은 실행기로 보내지 않고 바로 거절하는지 본다."""
        server = self.start_fake_runner()
        with self.assertRaises(SimpleError) as raised:
            RunnerClient(server.server_address[1], token=TOKEN).type_text("가" * 257)
        self.assertEqual(raised.exception.code, "runner_text_too_long")
        self.assertEqual(server.requests, [])

    # --- RunnerSession: 시작·인증 ---

    def test_session_passes_test_runner_prefixed_environment(self):
        """argv(세션 폴더의 DerivedData·결과 묶음 포함), TEST_RUNNER_ 환경 변수, 새 세션 그룹을 확인한다."""
        server = self.start_fake_runner()
        port = server.server_address[1]
        with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
            with self.session(port) as client:
                client.terminate()
        session_dir = session_dir_of(popen)
        self.assertEqual(session_dir.parent, self.log_path.parent)
        self.assertRegex(session_dir.name, r"runner-session-[0-9a-f]{32}\Z")
        self.assertEqual(popen.call_args.args[0], [
            "xcodebuild", "test-without-building", "-xctestrun", str(self.tmp / "runner" / "SimpleRunner.xctestrun"),
            "-destination", "id=UDID-1", "-only-testing:SimpleRunnerTests/RunnerSessionTests/testSession",
            "-derivedDataPath", str(session_dir / "dd"), "-resultBundlePath", str(session_dir / "result.xcresult")])
        kwargs = popen.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(env["TEST_RUNNER_REPROOF_TARGET_BUNDLE"], "io.reproof.example.mixedshop")
        self.assertEqual(env["TEST_RUNNER_REPROOF_RUNNER_PORT"], str(port))
        self.assertEqual(env["DEVELOPER_DIR"], "/Applications/Xcode.app/Contents/Developer")
        token, proof_key = secrets_of(popen)
        self.assertGreaterEqual(len(token), 43)  # secrets.token_urlsafe(32)는 43자 이상이다.
        self.assertGreaterEqual(len(proof_key), 43)
        self.assertNotEqual(token, proof_key)
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(self.log_path.exists())

    def test_session_sends_token_only_after_verified_handshake(self):
        """시작 handshake(status)에는 토큰이 없고 nonce만 있으며, 검증 뒤의 명령에만 Bearer 토큰이 실리는지 본다."""
        server = self.start_fake_runner()
        with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
            with self.session(server.server_address[1]) as client:
                client.terminate()
        token, _ = secrets_of(popen)
        actions = [request["body"]["action"] for request in server.requests]
        self.assertEqual(actions, ["status", "terminate", "stop"])
        handshake = server.requests[0]
        self.assertNotIn("Authorization", handshake["headers"])
        self.assertRegex(handshake["body"]["nonce"], NONCE_PATTERN)
        self.assertEqual([request["authorization"] for request in server.requests[1:]], [BEARER + token] * 2)

    def test_impostor_receives_no_secrets_during_startup(self):
        """포트를 가로챈 가짜 실행기는 시작 단계에서 토큰·proof 키를 하나도 받지 못하는지 본다."""
        server = self.start_fake_runner(_impostor(lambda body, env: status_document("guessed-key", body["nonce"])))
        error, popen = self.assert_start_failed(server)
        token, proof_key = secrets_of(popen)
        self.assertGreaterEqual(len(server.requests), 1)
        for request in server.requests:
            self.assertIsNone(request["authorization"])
            self.assertNotIn(token, json.dumps(request))
            self.assertNotIn(proof_key, json.dumps(request))
        self.assertNotIn(token, error.user_message())
        self.assertNotIn(proof_key, error.user_message())

    def test_each_session_uses_new_secrets_and_session_dir(self):
        """세션마다 토큰·proof 키·세션 폴더를 새로 만드는지 본다. 이전 세션 값으로 조작·사칭하지 못하게 하려는 것이다."""
        server = self.start_fake_runner()
        seen = []
        for _ in range(2):
            with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
                with self.session(server.server_address[1]):
                    seen.append((*secrets_of(popen), session_dir_of(popen)))
        self.assertTrue(all(first != second for first, second in zip(seen[0], seen[1])))

    def test_proof_key_never_leaves_the_process_environment(self):
        """proof 키는 xcodebuild 환경으로만 넘기고 HTTP 요청(헤더·본문)에는 절대 싣지 않는지 본다."""
        server = self.start_fake_runner()
        with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
            with self.session(server.server_address[1]) as client:
                client.tap_point(1, 2)
        _, proof_key = secrets_of(popen)
        self.assertEqual(len(server.requests), 3)
        for request in server.requests:
            self.assertNotIn(proof_key, json.dumps(request))

    def test_session_rejects_wrong_or_missing_status_proof(self):
        """proof가 다른 nonce·다른 키·다른 앱용이거나, 없거나, 문자열이 아니면 runner_start_failed인지 본다.

        "wrong-nonce": 예전에 받아 둔 proof를 재사용하는 가짜 실행기도 통과하지 못해야 한다.
        """
        impostors = {
            "wrong-nonce": lambda body, env: status_document(proof_key_of(env), "00" * 32),
            "unrelated-key": lambda body, env: status_document("guessed-key", body["nonce"]),
            "other-bundle-message": lambda body, env: status_document(
                proof_key_of(env), body["nonce"], proof=expected_proof(proof_key_of(env), "com.other.app", body["nonce"])),
            "missing": lambda body, env: {k: v for k, v in status_document(proof_key_of(env), body["nonce"]).items()
                                          if k != "proof"},
            "not-string": lambda body, env: status_document(proof_key_of(env), body["nonce"], proof=123),
        }
        for label, make_status in impostors.items():
            with self.subTest(label=label):
                server = self.start_fake_runner(_impostor(make_status))
                self.assert_start_failed(server)
                self.assertEqual({request["body"]["action"] for request in server.requests}, {"status"})

    def test_session_rejects_runner_for_other_target_bundle(self):
        """proof가 맞아도 다른 앱을 구동하는 실행기면 연결하지 않고 runner_start_failed를 내는지 본다."""
        server = self.start_fake_runner(_impostor(
            lambda body, env: status_document(proof_key_of(env), body["nonce"], "com.other.app")))
        self.assert_start_failed(server)

    def test_session_start_failure_keeps_session_dir_and_points_to_log(self):
        """준비 전에 xcodebuild가 끝나면 그룹을 정리하고, 세션 폴더는 남기되 비밀 값이 있을 수 있다고 안내하는지 본다."""
        port = free_loopback_port()
        with patched_process(_fake_process(exit_code=65)) as (popen, killpg):
            with self.assertRaises(SimpleError) as raised:
                with self.session(port):
                    self.fail("시작에 실패한 세션의 본문이 실행되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_start_failed")
        self.assertEqual(raised.exception.log_path, self.log_path)
        self.assertIn(mock.call(4321, signal.SIGTERM), killpg.call_args_list)
        session_dir = session_dir_of(popen)
        self.assertTrue(session_dir.is_dir())
        self.assertIn(str(session_dir), raised.exception.remedy)
        self.assertIn("비밀", raised.exception.remedy)

    def test_unwritable_log_location_raises_simple_error(self):
        """로그 폴더를 만들 수 없으면(같은 이름의 파일이 있음) xcodebuild를 띄우지 않고 SimpleError를 내는지 본다."""
        self.log_path.parent.write_text("파일이 폴더 자리를 차지했다")
        with patched_process(_fake_process()) as (popen, _):
            with self.assertRaises(SimpleError) as raised:
                with self.session(free_loopback_port()):
                    self.fail("로그를 쓸 수 없으면 세션이 시작되면 안 됩니다.")
        self.assertEqual(raised.exception.code, "runner_files_unwritable")
        self.assertIn(str(self.log_path.parent), raised.exception.cause)
        popen.assert_not_called()

    def test_session_log_never_truncates_a_linked_file(self):
        """세션 로그 자리가 하드·심볼릭 링크여도 연결된 파일을 자르지 않고, 새 0600 파일을 그 자리에 둔다."""
        self.log_path.parent.mkdir(parents=True)
        victim = self.tmp / "victim.txt"
        for make in (os.link, os.symlink):
            with self.subTest(link=make.__name__):
                victim.write_text("건드리면 안 됨", encoding="utf-8")
                self.log_path.unlink(missing_ok=True)
                make(victim, self.log_path)
                session = self.session(free_loopback_port())
                stream = session._prepare_files()
                stream.write(b"[reproof] test\n")
                stream.close()
                self.assertEqual(victim.read_text(encoding="utf-8"), "건드리면 안 됨")
                info = os.lstat(self.log_path)
                self.assertEqual((stat.S_ISREG(info.st_mode), info.st_nlink, info.st_mode & 0o777), (True, 1, 0o600))
                self.assertEqual(self.log_path.read_bytes(), b"[reproof] test\n")

    # --- RunnerSession: 종료·정리 ---

    def test_session_exit_sends_stop_and_kills_process_group(self):
        """stop이 성공하면 xcodebuild가 스스로 끝나기를 최대 10초 기다린 뒤 그룹을 SIGTERM·SIGKILL로 정리하는지 본다."""
        server = self.start_fake_runner()
        process = _fake_process(exits_after_stop=True)
        with patched_process(process, server) as (_, killpg):
            with self.session(server.server_address[1]):
                self.assertEqual(killpg.call_args_list, [])
        self.assertEqual([request["body"]["action"] for request in server.requests], ["status", "stop"])
        self.assertEqual(process.wait.call_args_list,
                         [mock.call(timeout=10), mock.call(timeout=2), mock.call(timeout=10)])  # 자연 종료·SIGTERM·SIGKILL 뒤
        self.assertEqual(killpg.call_args_list, [mock.call(4321, signal.SIGTERM), mock.call(4321, signal.SIGKILL)])

    def test_session_exit_kills_group_when_runner_keeps_running(self):
        """stop 뒤 10초 안에 끝나지 않으면 SIGTERM, 2초 뒤 SIGKILL로 정리하고 그 사실을 로그에 남기는지 본다."""
        server = self.start_fake_runner()
        process = _fake_process(exits_after_stop=False)
        with patched_process(process, server) as (_, killpg):
            with self.session(server.server_address[1]):
                pass
        self.assertEqual(process.wait.call_args_list,
                         [mock.call(timeout=10), mock.call(timeout=2), mock.call(timeout=10)])  # 자연 종료·SIGTERM·SIGKILL 뒤
        self.assertEqual(killpg.call_args_list, [mock.call(4321, signal.SIGTERM), mock.call(4321, signal.SIGKILL)])
        self.assertEqual(self.log_path.read_text(encoding="utf-8").count("[reproof]"), 2)

    def test_successful_stop_removes_session_dir(self):
        """세션 폴더는 0700이고, stop이 성공하면 결과 묶음째 지워지는지 본다(비밀 값·입력 글자가 남지 않게)."""
        server = self.start_fake_runner()
        with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
            with self.session(server.server_address[1]):
                session_dir = session_dir_of(popen)
                self.assertEqual(stat.S_IMODE(session_dir.stat().st_mode), 0o700)
                bundle = session_dir / "result.xcresult"
                bundle.mkdir()
                (bundle / "StandardOutputAndStandardError.txt").write_text("입력한 글자")
        self.assertFalse(session_dir.exists())

    def test_failed_stop_keeps_session_dir_and_warns_about_secrets(self):
        """stop이 실패하면 세션 폴더를 남기고, 비밀 값이 있을 수 있다는 안내와 함께 runner_stop_failed를 내는지 본다."""
        def reject_stop(body, token, env):
            if body.get("action") == "stop":
                return 401, {"ok": False, "error": "unauthorized"}
            return _honest_runner(body, token, env)

        server = self.start_fake_runner(reject_stop)
        process = _fake_process(exits_after_stop=True)
        with patched_process(process, server) as (popen, _):
            with self.assertRaises(SimpleError) as raised:
                with self.session(server.server_address[1]):
                    pass
        session_dir = session_dir_of(popen)
        self.assertEqual(raised.exception.code, "runner_stop_failed")
        self.assertTrue(session_dir.is_dir())
        self.assertIn(str(session_dir), raised.exception.remedy)
        self.assertIn("비밀", raised.exception.remedy)
        self.assertEqual(process.wait.call_args_list[0], mock.call(timeout=2))  # stop 실패면 자연 종료를 기다리지 않는다.
        written = self.log_path.read_text(encoding="utf-8")
        self.assertGreaterEqual(written.count("[reproof]"), 1)  # 진단이 실제로 쓰였는지 확인해 빈 로그로 통과하지 않게 한다.
        for secret in secrets_of(popen):
            self.assertNotIn(secret, written)
            self.assertNotIn(secret, raised.exception.user_message())

    def test_failed_stop_does_not_hide_the_original_error(self):
        """with 본문이 예외로 끝나면 stop이 실패해도 원래 예외가 전파되고, 남긴 세션 폴더 안내는 로그에 남는지 본다.

        왜 로그인가: 이때 runner_stop_failed는 원래 예외를 가리지 않도록 버려지므로, 비밀 값이 남은 폴더를
        알릴 곳이 로그뿐이다.
        """
        def reject_stop(body, token, env):
            if body.get("action") == "stop":
                return 500, {"ok": False, "error": "boom"}
            return _honest_runner(body, token, env)

        server = self.start_fake_runner(reject_stop)
        with patched_process(_fake_process(exits_after_stop=True), server) as (popen, _):
            with self.assertRaises(KeyError):
                with self.session(server.server_address[1]):
                    raise KeyError("본문 오류")
        session_dir = session_dir_of(popen)
        self.assertTrue(session_dir.is_dir())
        written = self.log_path.read_text(encoding="utf-8")
        notice = next((line for line in written.splitlines() if str(session_dir) in line), "")
        self.assertIn("비밀", notice)

    def test_interrupted_exit_still_cleans_up_process_group(self):
        """정리 도중 KeyboardInterrupt·로그 쓰기 OSError가 나도 SIGTERM·SIGKILL로 그룹을 정리하고 예외를 다시 던지는지 본다.

        왜: start_new_session=True라 터미널의 Ctrl-C가 xcodebuild에 닿지 않는다. 정리를 건너뛰면 Simulator와
        포트를 쥔 세션이 최대 6시간 남는다.
        """
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        def stop_then_slow_exit_with_broken_log(session):
            session._note = mock.Mock(side_effect=OSError("로그 디스크 가득 참"))

        cases = {
            "stop-interrupted": (KeyboardInterrupt, lambda session: setattr(session, "_send_stop", interrupt), True),
            "natural-wait-interrupted": (KeyboardInterrupt, lambda session: None, "interrupt-wait"),
            "note-oserror-during-group-stop": (OSError, stop_then_slow_exit_with_broken_log, False),
        }
        for label, (expected, sabotage, exits) in cases.items():
            with self.subTest(label=label):
                server = self.start_fake_runner()
                process = _fake_process(exits_after_stop=exits is True)
                if exits == "interrupt-wait":
                    process.wait.side_effect = lambda timeout=None: (_ for _ in ()).throw(KeyboardInterrupt) \
                        if timeout == 10 else 0
                with patched_process(process, server) as (_, killpg):
                    session = self.session(server.server_address[1])
                    with self.assertRaises(expected):
                        with session:
                            sabotage(session)
                self.assertEqual(killpg.call_args_list[-2:],
                                 [mock.call(4321, signal.SIGTERM), mock.call(4321, signal.SIGKILL)])
                self.assertTrue(session._log.closed)

    def test_zombie_leader_does_not_skip_cleanup_or_hide_stop_failure(self):
        """xcodebuild가 스스로 끝나 회수 전(좀비)인 그룹에서도 정리를 끝내고 runner_stop_failed를 내는지 본다.

        왜: macOS는 좀비만 남은 그룹의 killpg에 EPERM을 낸다. 예전에는 PermissionError가 SIGKILL 정리와
        세션 폴더 처리를 건너뛰고 원래 오류(stop 실패)를 가렸다. 실제 프로세스와 실제 커널 EPERM으로 확인한다.
        """
        def reject_stop(body, token, env):
            if body.get("action") == "stop":
                return 401, {"ok": False, "error": "unauthorized"}
            return _honest_runner(body, token, env)

        server = self.start_fake_runner(reject_stop)
        spawned, environments = [], []
        real_popen = subprocess.Popen

        def spawn_short_lived(argv, **kwargs):
            """세션 xcodebuild 대신 곧 끝나는 셸을 같은 옵션(새 세션 그룹 포함)으로 띄운다."""
            environments.append(kwargs["env"])
            spawned.append(real_popen(["/bin/sh", "-c", "sleep 0.3"], **kwargs))
            return spawned[-1]

        server.session_env = lambda: environments[0] if environments else None
        with mock.patch.object(runner_client.subprocess, "Popen", side_effect=spawn_short_lived), \
                mock.patch.object(runner_client, "build_default_environment", return_value={}):
            with self.assertRaises(SimpleError) as raised:
                with self.session(server.server_address[1]):
                    wait_until_zombie(spawned[0])  # 리더가 끝나 좀비가 될 때까지 기다린다(회수하지 않음, OS별 방법).
        self.assertEqual(raised.exception.code, "runner_stop_failed")
        self.assertIsNotNone(spawned[0].returncode)

    def test_group_that_stays_eperm_is_noted_and_cleanup_continues(self):
        """재시도 한도 뒤에도 EPERM이면 로그에 남기고 SIGKILL·회수·세션 폴더 정리를 이어 가며 원래 예외를 지키는지 본다."""
        server = self.start_fake_runner()
        process = _fake_process(exits_after_stop=True)
        with patched_process(process, server) as (popen, killpg), \
                mock.patch.object(repair, "_GROUP_SIGNAL_RETRY_SECONDS", 0.1, create=True):
            killpg.side_effect = PermissionError(errno.EPERM, "Operation not permitted")
            with self.assertRaises(KeyError):
                with self.session(server.server_address[1]):
                    raise KeyError("본문 오류")
        signals = [call.args[1] for call in killpg.call_args_list]
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)
        self.assertEqual(process.wait.call_args_list[-1], mock.call(timeout=10))
        self.assertFalse(session_dir_of(popen).exists())
        self.assertIn("EPERM", self.log_path.read_text(encoding="utf-8"))

    def test_wait_after_sigkill_is_bounded_and_cleanup_finishes(self):
        """SIGKILL 뒤에도 xcodebuild가 끝나지 않으면 제한 없이 기다리지 않고(최대 10초) 로그에 남긴 뒤 정리를 마친다.

        왜: 제한 없는 wait()는 프로젝트·Simulator 잠금을 쥔 채 inspect를 영원히 멈추게 한다.
        """
        server = self.start_fake_runner()
        process = mock.Mock(pid=4321)
        process.poll.return_value = None

        def stuck_wait(timeout=None):
            if timeout is None:
                raise AssertionError("제한 없는 wait()를 부르면 안 됩니다.")
            raise subprocess.TimeoutExpired("xcodebuild", timeout)

        process.wait.side_effect = stuck_wait
        with patched_process(process, server) as (popen, killpg):
            with self.session(server.server_address[1]):
                pass
        self.assertEqual(process.wait.call_args_list[-1], mock.call(timeout=10))
        self.assertIn("SIGKILL 뒤", self.log_path.read_text(encoding="utf-8"))
        self.assertFalse(session_dir_of(popen).exists())

    # --- 빌드·리소스 ---

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

    def test_build_runner_output_folder_is_private(self):
        """실행기 폴더(runner-src·runner-dd·캐시를 담음)는 0700이다. 기존 폴더도 0700으로 좁힌다.

        왜: 캐시의 실행기 바이너리는 세션 토큰·proof 키를 환경 변수로 받는다. 다른 계정이 바꿔치기할 수 있으면 안 된다.
        """
        def build(argv, cwd, env_extra):
            products = Path(argv[argv.index("-derivedDataPath") + 1]) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "SimpleRunner.xctestrun").write_text("plist")
            return ""

        output = self.tmp / "out"
        output.mkdir(mode=0o755)
        os.chmod(output, 0o755)
        build_runner(output, FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.0\n"),
                                             (("xcodebuild", "build-for-testing"), build)]))
        self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)

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

    def test_build_runner_cache_key_changes_with_runner_recipe_version(self):
        """실행기 빌드 방법 버전(RUNNER_RECIPE_VERSION)이 바뀌면 캐시 키가 달라져 옛 실행기를 재사용하지 않는다."""
        def build(argv, cwd, env_extra):
            products = Path(argv[argv.index("-derivedDataPath") + 1]) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "SimpleRunner.xctestrun").write_text("plist")
            return ""

        output = self.tmp / "out"
        responses = [(("xcodebuild", "-version"), "Xcode 27.0\n"), (("xcodebuild", "build-for-testing"), build)]
        older = build_runner(output, FakeToolRunner(responses))
        with mock.patch.object(runner_client, "RUNNER_RECIPE_VERSION", 99, create=True):
            newer = build_runner(output, FakeToolRunner(responses))
        self.assertNotEqual(older.xctestrun.parent, newer.xctestrun.parent)

    def test_build_runner_refuses_symlinked_runner_folder(self):
        """runner-src·runner-dd 자리에 링크가 있으면 링크 너머를 지우지 않고 workspace_symlink로 멈춘다."""
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("지우면 안 됨", encoding="utf-8")
        output = self.tmp / "out"
        output.mkdir()
        (output / "runner-src").symlink_to(victim)
        with self.assertRaises(SimpleError) as raised:
            build_runner(output, FakeToolRunner([]))
        self.assertEqual(raised.exception.code, "workspace_symlink")
        self.assertTrue((victim / "keep.txt").is_file())

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
