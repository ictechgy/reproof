"""범용 실행기를 실제 Xcode·Simulator로 검증하는 native 테스트(Xcode 없으면 skip하지 않고 실패한다).

reproof 전용 Simulator `reproof-mixedshop-<MixedShop 경로 해시>`만 만들거나 초기화하고, 끝나면 끈다(기기 자체는 남긴다).
사용자 DerivedData(~/Library/Developer/Xcode/DerivedData)는 읽기만 하고 아무것도 지우지 않는다.
"""
from __future__ import annotations

import http.client
import json
import secrets
import select
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path

from reproof.simple.config import ProjectConfig, SimulatorSettings
from reproof.simple.hierarchy import iter_elements
from reproof.simple.locator import capture_evidence, resolve
from reproof.simple.pipeline import prepare_build
from reproof.simple.process import HostToolRunner
from reproof.simple.runner_client import RunnerSession, build_runner, free_loopback_port
from reproof.simple.locking import simulator_lock
from reproof.simple.simulator import SimulatorManager, launch_locale_settings, owned_device_name

MIXEDSHOP = Path(__file__).resolve().parents[1] / "examples" / "simple-ios" / "MixedShop"
BUNDLE = "io.reproof.example.mixedshop"
USER_DERIVED_DATA = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"
# 실행기의 요청 전체 제한 시간(초)과, 측정 오차를 감안한 허용 상한(초).
REQUEST_DEADLINE_SECONDS = 10
REQUEST_DEADLINE_ALLOWANCE = 13


def mixedshop_config() -> ProjectConfig:
    """test_simple_native_build와 같은 MixedShop 설정을 만든다(XcodeGen·로컬 패키지)."""
    return ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                         generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                         bundle_id=BUNDLE, test_scheme="MixedShopTests",
                         simulator=SimulatorSettings("iPhone 16", "latest"))


def raw_command(port: int, headers: dict[str, str], body: dict, *,
                content_type: str = "application/json") -> tuple[int, dict]:
    """Python 클라이언트를 거치지 않고 요청을 보내 (상태 코드, 응답 JSON)을 돌려준다.

    왜: 클라이언트는 항상 맞는 헤더를 보내므로, 실행기의 인증·Host·Origin·Content-Type 검사는 원시 요청으로만 확인할 수 있다.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", "/command", body=json.dumps(body).encode("utf-8"),
                           headers={"Content-Type": content_type, **headers})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def status_body() -> dict:
    """새 nonce를 담은 handshake(status) 본문."""
    return {"action": "status", "nonce": secrets.token_hex(32)}


def simple_runner_derived_data() -> set[str]:
    """사용자 DerivedData의 SimpleRunner-* 아래 모든 경로(상대 경로)를 모은다. 읽기만 한다."""
    if not USER_DERIVED_DATA.is_dir():
        return set()
    return {str(path.relative_to(USER_DERIVED_DATA)) for pattern in ("SimpleRunner-*", "SimpleRunner-*/**/*")
            for path in USER_DERIVED_DATA.glob(pattern)}


def files_containing(root: Path, values: tuple[str, ...]) -> list[str]:
    """root 아래 일반 파일 중 values 중 하나라도 바이트로 들어 있는 파일 경로 목록. 링크는 따라가지 않는다."""
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(value.encode("utf-8") in data for value in values):
                found.append(str(path))
    return found


def seconds_until_server_closes(port: int, limit: float) -> float | None:
    """요청을 끝내지 않고 1초마다 한 바이트씩 보내며 서버가 연결을 끊을 때까지 걸린 시간을 잰다. limit초 넘으면 None.

    왜: 조각마다 대기 시간을 다시 늘리는 서버는 이런 느린 요청(slowloris)으로 연결 자리를 모두 잡힐 수 있다.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        started = time.monotonic()
        partial = f"POST /command HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Slow: ".encode("ascii")
        for index in range(int(limit) + 1):
            try:
                connection.sendall(partial[index:index + 1] or b"x")
                readable, _, _ = select.select([connection], [], [], 1.0)
                if readable and connection.recv(1) == b"":
                    return time.monotonic() - started
            except (BrokenPipeError, ConnectionResetError):
                return time.monotonic() - started
    return None


class SimpleNativeRunnerTests(unittest.TestCase):
    """실행기가 UIKit·UIKit에 끼운 SwiftUI 요소를 계층으로 내보내고 좌표 탭이 앱을 움직이는지 확인한다."""

    def setUp(self) -> None:
        """임시 작업 공간(경로에 공백·한글 포함)에 실행기와 MixedShop을 빌드하고 전용 Simulator를 깨끗하게 준비한다."""
        self._tmp = tempfile.TemporaryDirectory(prefix="reproof 실행기 ")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.runner = HostToolRunner()
        self.config = mixedshop_config()
        self.products = build_runner(self.workspace / "runner", self.runner)
        prepared = prepare_build(MIXEDSHOP, self.config, workspace_root=self.workspace, runner=self.runner,
                                 progress=lambda _: None)
        self.device_name = owned_device_name(self.config.name, MIXEDSHOP)
        lock = simulator_lock(self.device_name)  # inspect처럼 기기 준비부터 테스트 끝까지 같은 기기 잠금을 쥔다.
        lock.__enter__()
        self.addCleanup(lock.__exit__, None, None, None)
        self.udid = self._prepare_simulator(prepared.products.app_path)

    def _prepare_simulator(self, app_path: Path) -> str:
        """reproof-mixedshop-<해시>를 만들거나 재사용해 초기화·표시 고정·앱 설치를 하고 udid를 돌려준다."""
        simulators = SimulatorManager(self.runner)
        udid = simulators.ensure_device(self.device_name, self.config.simulator)
        self.addCleanup(self._shutdown, udid)
        simulators.reset(udid)
        simulators.apply_presentation(udid)
        simulators.install_app(udid, app_path)
        return udid

    def _shutdown(self, udid: str) -> None:
        """테스트가 켠 전용 기기를 끈다. 이름 접두사를 다시 확인해 다른 기기는 절대 건드리지 않는다."""
        names = self.runner.run(["xcrun", "simctl", "list", "devices", udid], cwd=Path.cwd(), timeout=60)
        self.assertIn(f"{self.device_name} ({udid})", names)
        if "(Booted)" in names:
            self.runner.run(["xcrun", "simctl", "shutdown", udid], cwd=Path.cwd(), timeout=120)

    def assert_request_guards(self, port: int, token: str) -> None:
        """토큰 없는 명령·틀린 토큰은 401, 토큰 없는 handshake는 proof만, Origin·다른 Host는 403, JSON이 아니면 415,
        인증 전 큰 본문은 413, 깊은 중첩 JSON은 400인지 본다."""
        unauthorized = (401, {"ok": False, "error": "unauthorized"})
        tap = {"action": "tap", "x": 1, "y": 1}
        self.assertEqual(raw_command(port, {}, tap), unauthorized)
        self.assertEqual(raw_command(port, {"Authorization": "Bearer wrong"}, status_body()), unauthorized)
        code, handshake = raw_command(port, {}, status_body())
        self.assertEqual((code, set(handshake)), (200, {"ok", "targetBundle", "targetState", "proof"}))
        self.assertEqual(raw_command(port, {}, {"action": "status"}), (200, {"ok": False, "error": "invalid_nonce"}))
        bearer = {"Authorization": f"Bearer {token}"}
        self.assertEqual(raw_command(port, {**bearer, "Origin": "http://evil.example"}, tap)[0], 403)
        self.assertEqual(raw_command(port, {**bearer, "Host": f"evil.example:{port}"}, tap)[0], 403)
        self.assertEqual(raw_command(port, bearer, tap, content_type="text/plain")[0], 415)
        self.assertEqual(raw_command(port, {**bearer, "Host": f"localhost:{port}"}, status_body())[0], 200)
        # 인증 전 요청은 본문 1 KiB까지만 받고, 인증된 요청도 아주 깊은 JSON은 파싱 전에 거절한다.
        self.assertEqual(raw_command(port, {}, {**status_body(), "pad": "x" * 2048}),
                         (413, {"ok": False, "error": "body_too_large"}))
        nested: list = []
        for _ in range(100):
            nested = [nested]
        self.assertEqual(raw_command(port, bearer, {"action": "tap", "x": nested}),
                         (400, {"ok": False, "error": "json_too_deep"}))

    def assert_session_dir_holds_xcode_results(self, session: RunnerSession) -> None:
        """세션 폴더가 0700이고 xcodebuild 결과 묶음이 실제로 그 안에 쓰이는지 확인한다."""
        self.assertEqual(stat.S_IMODE(session.session_dir.stat().st_mode), 0o700)
        self.assertTrue((session.session_dir / "result.xcresult").exists())

    def test_hierarchy_contains_uikit_and_embedded_swiftui_elements(self) -> None:
        """요청 검사 뒤, '담기' 탭으로 수량이 늘고 '옵션' 탭으로 들어간 SwiftUI 화면에 '추가' 버튼 3개가 보이는지 확인한다.

        끝나면 세션 폴더가 지워지고, 사용자 DerivedData에 새 항목이 없으며, 로그 폴더 어디에도 비밀 값이 없어야 한다.
        """
        logs = self.workspace / "logs"
        port = free_loopback_port()
        derived_data_before = simple_runner_derived_data()
        session = RunnerSession(self.products, udid=self.udid, target_bundle=BUNDLE, port=port,
                                log_path=logs / "runner-session.log")
        with session as client:
            # 테스트 전용으로 비공개 비밀 값을 읽는다: 인증이 통과한 상태에서도 Host·Origin·Content-Type 검사가
            # 각각 동작하는지 보려면 맞는 토큰이 필요하고, 파일에 비밀 값이 없는지도 확인해야 한다.
            token, proof_key = session._token, session._proof_key
            self.assert_session_dir_holds_xcode_results(session)
            self.assert_request_guards(port, token)
            closed_after = seconds_until_server_closes(port, REQUEST_DEADLINE_ALLOWANCE + 3)
            self.assertIsNotNone(closed_after)
            self.assertLess(closed_after, REQUEST_DEADLINE_ALLOWANCE)
            client.launch(*launch_locale_settings(self.config.simulator))
            root = client.hierarchy()
            self.assertTrue(any(e.label == "담기" and e.element_type == "button" for e in iter_elements(root)))
            add = next(e for e in iter_elements(root) if e.label == "담기")
            evidence = capture_evidence(root, add)
            client.tap_point(*resolve(client.hierarchy(), evidence).element.frame.center())
            self.assertTrue(any(e.label == "담은 수 1" for e in iter_elements(client.hierarchy())))
            options = next(e for e in iter_elements(client.hierarchy()) if e.label == "옵션")
            client.tap_point(*options.frame.center())
            self.assertEqual(sum(1 for e in iter_elements(client.hierarchy())
                                 if e.label == "추가" and e.element_type == "button"), 3)
        written = (logs / "runner-session.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("RunnerSessionTests", written)  # xcodebuild 출력이 실제로 로그에 있는지 먼저 확인한다.
        self.assertNotIn("BUILD INTERRUPTED", written)  # stop 뒤 xcodebuild가 스스로 끝났다.
        self.assertFalse(session.session_dir.exists())
        self.assertEqual(files_containing(logs, (token, proof_key)), [])
        self.assertEqual(simple_runner_derived_data(), derived_data_before)


if __name__ == "__main__":
    unittest.main()
