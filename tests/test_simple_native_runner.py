"""범용 실행기를 실제 Xcode·Simulator로 검증하는 native 테스트(Xcode 없으면 skip하지 않고 실패한다).

reproof 전용 Simulator `reproof-mixedshop`만 만들거나 초기화하고, 끝나면 끈다(기기 자체는 남긴다).
"""
from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path

from reproof.simple.config import ProjectConfig, SimulatorSettings
from reproof.simple.hierarchy import iter_elements
from reproof.simple.locator import capture_evidence, resolve
from reproof.simple.pipeline import prepare_build
from reproof.simple.process import HostToolRunner
from reproof.simple.runner_client import RunnerSession, build_runner, free_loopback_port
from reproof.simple.simulator import OWNED_PREFIX, SimulatorManager, launch_locale_settings

MIXEDSHOP = Path(__file__).resolve().parents[1] / "examples" / "simple-ios" / "MixedShop"
BUNDLE = "io.reproof.example.mixedshop"


def mixedshop_config() -> ProjectConfig:
    """test_simple_native_build와 같은 MixedShop 설정을 만든다(XcodeGen·로컬 패키지)."""
    return ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                         generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                         bundle_id=BUNDLE, test_scheme="MixedShopTests",
                         simulator=SimulatorSettings("iPhone 16", "latest"))


def raw_command(port: int, headers: dict[str, str], *, content_type: str = "application/json") -> tuple[int, dict]:
    """Python 클라이언트를 거치지 않고 status 요청을 보내 (상태 코드, 응답 JSON)을 돌려준다.

    왜: 클라이언트는 항상 맞는 헤더를 보내므로, 실행기의 인증·Host·Origin·Content-Type 검사는 원시 요청으로만 확인할 수 있다.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", "/command", body=b'{"action": "status"}',
                           headers={"Content-Type": content_type, **headers})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


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
        self.udid = self._prepare_simulator(prepared.products.app_path)

    def _prepare_simulator(self, app_path: Path) -> str:
        """reproof-mixedshop을 만들거나 재사용해 초기화·표시 고정·앱 설치를 하고 udid를 돌려준다."""
        simulators = SimulatorManager(self.runner)
        udid = simulators.ensure_device(self.config.name, self.config.simulator)
        self.addCleanup(self._shutdown, udid)
        simulators.reset(udid)
        simulators.apply_presentation(udid)
        simulators.install_app(udid, app_path)
        return udid

    def _shutdown(self, udid: str) -> None:
        """테스트가 켠 전용 기기를 끈다. 이름 접두사를 다시 확인해 다른 기기는 절대 건드리지 않는다."""
        names = self.runner.run(["xcrun", "simctl", "list", "devices", udid], cwd=Path.cwd(), timeout=60)
        self.assertIn(f"{OWNED_PREFIX}{self.config.name} ({udid})", names)
        if "(Booted)" in names:
            self.runner.run(["xcrun", "simctl", "shutdown", udid], cwd=Path.cwd(), timeout=120)

    def assert_request_guards(self, port: int, token: str) -> None:
        """토큰 없음·틀린 토큰은 401, Origin·다른 Host는 맞는 토큰이어도 403, JSON이 아니면 415인지 확인한다."""
        unauthorized = (401, {"ok": False, "error": "unauthorized"})
        self.assertEqual(raw_command(port, {}), unauthorized)
        self.assertEqual(raw_command(port, {"Authorization": "Bearer wrong"}), unauthorized)
        bearer = {"Authorization": f"Bearer {token}"}
        self.assertEqual(raw_command(port, {**bearer, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(raw_command(port, {**bearer, "Host": f"evil.example:{port}"})[0], 403)
        self.assertEqual(raw_command(port, bearer, content_type="text/plain")[0], 415)
        self.assertEqual(raw_command(port, {**bearer, "Host": f"localhost:{port}"})[0], 200)

    def test_hierarchy_contains_uikit_and_embedded_swiftui_elements(self) -> None:
        """요청 검사 뒤, '담기' 탭으로 수량이 늘고 '옵션' 탭으로 들어간 SwiftUI 화면에 '추가' 버튼 3개가 보이는지 확인한다."""
        log = self.workspace / "logs" / "runner-session.log"
        port = free_loopback_port()
        session = RunnerSession(self.products, udid=self.udid, target_bundle=BUNDLE, port=port, log_path=log)
        with session as client:
            # 테스트 전용으로 비공개 비밀 값을 읽는다: 인증이 통과한 상태에서도 Host·Origin·Content-Type 검사가
            # 각각 동작하는지 보려면 맞는 토큰이 필요하고, 로그에 비밀 값이 없는지도 확인해야 한다.
            token, proof_key = session._token, session._proof_key
            self.assert_request_guards(port, token)
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
        written = log.read_text(encoding="utf-8", errors="replace")
        self.assertIn("RunnerSessionTests", written)  # xcodebuild 출력이 실제로 로그에 있는지 먼저 확인한다.
        self.assertNotIn(token, written)
        self.assertNotIn(proof_key, written)


if __name__ == "__main__":
    unittest.main()
