"""범용 XCUITest 실행기(ios/SimpleRunner)의 빌드 캐시, 세션 수명, HTTP 명령 클라이언트.

실행기는 bundle id로 지정한 아무 앱이나 구동하고 127.0.0.1에서 작은 명령 API를 연다. 요소를 찾는
로직은 모두 Python(hierarchy·locator)에 두고, 실행기는 계층 내보내기와 좌표 입력만 맡는다.
왜: 회사 프로젝트를 고치지 않고도 설치된 앱을 그대로 다루기 위해서다.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..repair import signal_process_group
from ..resources import ResourceError, read_resource
from .errors import SimpleError
from .hierarchy import Element, parse_hierarchy
from .locking import runner_lock
from .process import ToolRunner, build_default_environment
from .workcopy import tree_digest

# 실행기 소스의 리소스 경로 접두사. 복사할 때 이 부분을 떼고 runner-src 아래에 둔다.
RUNNER_PREFIX = "ios/SimpleRunner/"
# 실행기 소스 파일 목록(distribution-resources.json과 같아야 한다. 테스트가 확인한다).
# 왜 고정 목록인가: 설치본에서도 같은 파일을 해시 검증된 read_resource로만 읽기 위해서다.
RUNNER_FILES = (
    "ios/SimpleRunner/RunnerHost/AppDelegate.swift",
    "ios/SimpleRunner/RunnerHost/Info.plist",
    "ios/SimpleRunner/RunnerTests/CommandServer.swift",
    "ios/SimpleRunner/RunnerTests/HierarchyExport.swift",
    "ios/SimpleRunner/RunnerTests/Info.plist",
    "ios/SimpleRunner/RunnerTests/RunnerSessionTests.swift",
    "ios/SimpleRunner/SimpleRunner.xcodeproj/project.pbxproj",
    "ios/SimpleRunner/SimpleRunner.xcodeproj/project.xcworkspace/contents.xcworkspacedata",
    "ios/SimpleRunner/SimpleRunner.xcodeproj/xcshareddata/xcschemes/SimpleRunner.xcscheme",
    "ios/SimpleRunner/project.yml",
)
# xcodebuild -only-testing에 넘기는 세션 테스트 식별자.
SESSION_TEST = "SimpleRunnerTests/RunnerSessionTests/testSession"
# 응답 본문 상한. screenshot(base64 PNG)이 커서 넉넉히 둔다.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# type 명령 한 번에 보낼 수 있는 최대 글자 수(실행기와 같은 값).
MAX_TEXT_LENGTH = 256
_VERSION_TIMEOUT = 60
_BUILD_TIMEOUT = 1800
# 시작 대기·stop 전송에 쓰는 짧은 요청 제한 시간(초)과 status 재시도 간격(초).
_PROBE_TIMEOUT = 5.0
_POLL_INTERVAL = 0.5
# SIGTERM 뒤 SIGKILL까지 기다리는 시간(초).
_TERMINATE_GRACE_SECONDS = 2
# stop 성공 뒤 xcodebuild가 스스로 끝나기를 기다리는 시간(초). 결과 정리를 마치게 해 BUILD INTERRUPTED를 피한다.
_NATURAL_EXIT_SECONDS = 10
# duration이 있는 제스처 명령의 응답 대기 여유(초). 제스처 시간 + 이 값까지 기다린다.
_GESTURE_TIMEOUT_MARGIN = 10.0
# 세션 폴더 이름 접두사. log_path 옆에 `runner-session-<uuid>`로 만든다.
_SESSION_DIR_PREFIX = "runner-session-"
# handshake nonce 바이트 수(hex 64자).
_NONCE_BYTES = 32
# 실행기가 보낸 문자열을 안내문에 넣을 때의 최대 글자 수와, 글자·숫자 외에 허용하는 문장 부호.
_RUNNER_TEXT_LIMIT = 120
_RUNNER_TEXT_PUNCTUATION = frozenset(" _-.:,;/()[]'\"=+#|?!%@")
# 세션 xcodebuild에 물려주는 사용자 환경 변수. repair.run_command와 같은 최소 집합이다.
_INHERITED_ENVIRONMENT = ("HOME", "USER", "LANG", "LC_ALL", "PATH")
# handshake proof HMAC 메시지 접두사. 실행기(Swift)와 같은 값이어야 한다. 메시지는 접두사 + 앱 + ":" + nonce다.
STATUS_PROOF_PREFIX = "reproof-runner-status:"
# 세션 토큰·proof 키 엔트로피(바이트). secrets.token_urlsafe(32)는 43자 이상의 URL-safe 문자열이다.
_TOKEN_BYTES = 32
# http_proxy 같은 환경 변수를 따르지 않는 opener. loopback 요청이 프록시로 새면 안 되기 때문이다.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class RunnerProducts:
    """빌드된 실행기. xctestrun: `test-without-building -xctestrun`에 넘길 파일 경로."""

    xctestrun: Path


def runner_build_argv(derived_data: Path) -> list[str]:
    """실행기 build-for-testing argv를 만든다. 서명은 끈다(Simulator 전용이고 개발자 인증서를 건드리지 않기 위해)."""
    return ["xcodebuild", "build-for-testing", "-project", "SimpleRunner.xcodeproj", "-scheme", "SimpleRunner",
            "-sdk", "iphonesimulator", "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", str(derived_data), "CODE_SIGNING_ALLOWED=NO"]


def runner_cache_key(xcode_version: str, source_digest: str) -> str:
    """(xcodebuild -version 출력, 실행기 소스 tree_digest)의 sha256 앞 16자. Xcode나 소스가 바뀌면 키도 바뀐다."""
    return hashlib.sha256(f"{xcode_version}\n{source_digest}".encode("utf-8")).hexdigest()[:16]


def _remove_tree(path: Path) -> None:
    """path가 있으면 트리째 지운다. 실행기 폴더는 이 모듈이 만든 것뿐이라 권한 복구는 필요 없다."""
    if path.exists():
        shutil.rmtree(path)


def _read_runner_file(name: str) -> bytes:
    """실행기 소스 파일 하나를 리소스로 읽는다. 없거나 손상됐으면 runner_source_missing."""
    try:
        return read_resource(name)
    except ResourceError as error:
        raise SimpleError("runner_source_missing", f"실행기 소스 파일({name})을 읽지 못했습니다.",
                          "reproof를 다시 설치하거나, 저장소의 ios/SimpleRunner 폴더가 온전한지 확인한 뒤 "
                          "다시 실행하세요.") from error


def copy_runner_source(destination: Path) -> None:
    """실행기 소스를 destination에 새로 복사한다. 남은 이전 사본은 먼저 지운다(캐시 키가 섞이지 않게)."""
    contents = {name: _read_runner_file(name) for name in RUNNER_FILES}
    _remove_tree(destination)
    for name, raw in contents.items():
        target = destination / name[len(RUNNER_PREFIX):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def _single_xctestrun(directory: Path) -> Path | None:
    """directory 바로 아래 .xctestrun이 정확히 하나면 그 경로, 아니면 None."""
    matches = sorted(directory.glob("*.xctestrun")) if directory.is_dir() else []
    return matches[0] if len(matches) == 1 else None


def build_runner(output_root: Path, runner: ToolRunner) -> RunnerProducts:
    """실행기를 Simulator용으로 빌드하고, 같은 캐시 키의 완성 빌드가 있으면 재사용한다.

    흐름: 리소스를 `runner-src`에 복사 → 키 계산 → `runner/<key>/`에 .xctestrun이 있으면 반환,
    없으면 `runner-dd`에서 빌드해 Products를 `runner/<key>.partial`로 복사한 뒤 rename한다.
    왜 rename인가: 중간에 끊긴 빌드가 완성된 캐시로 보이지 않게 하려는 것이다.
    잠금: 전체를 실행기 잠금(runner_lock) 안에서 한다. 왜: 다른 터미널의 실행이 runner-src·runner-dd를 지우고 다시 쓰는
    도중에 섞이지 않게 하려는 것이다. 잠겨 있으면 아무것도 하지 않고 runner_busy를 낸다.
    """
    output_root = output_root.expanduser().absolute()
    with runner_lock(output_root):
        return _build_runner_locked(output_root, runner)


def _build_runner_locked(output_root: Path, runner: ToolRunner) -> RunnerProducts:
    """실행기 잠금을 쥔 상태에서 소스 복사·키 계산·캐시 확인·빌드를 한다(build_runner 참고)."""
    source = output_root / "runner-src"
    copy_runner_source(source)
    version = runner.run(["xcodebuild", "-version"], cwd=source, timeout=_VERSION_TIMEOUT).strip()
    final = output_root / "runner" / runner_cache_key(version, tree_digest(source))
    cached = _single_xctestrun(final)
    if cached is not None:
        return RunnerProducts(cached)
    return _build_into_cache(source, output_root, final, runner)


def _build_into_cache(source: Path, output_root: Path, final: Path, runner: ToolRunner) -> RunnerProducts:
    """runner-dd를 비우고 빌드한 뒤 Products를 partial에 복사하고 final로 rename해 RunnerProducts를 돌려준다.

    runner-dd를 비우는 이유: 옛 Xcode가 남긴 다른 이름의 .xctestrun이 섞여 하나로 고를 수 없게 되는 것을 막는다.
    Products 폴더째 복사하는 이유: .xctestrun 안의 경로가 `__TESTROOT__`(자기 폴더) 기준이라 함께 있어야 유효하다.
    """
    derived, log_path = output_root / "runner-dd", output_root / "runner-build.log"
    _remove_tree(derived)
    runner.run(runner_build_argv(derived), cwd=source, timeout=_BUILD_TIMEOUT, log_path=log_path)
    built = derived / "Build" / "Products"
    if _single_xctestrun(built) is None:
        raise SimpleError("runner_build_invalid", "실행기 빌드 결과에서 .xctestrun 파일을 하나로 특정하지 못했습니다.",
                          "로그를 확인하고 출력 폴더의 runner-dd를 지운 뒤 다시 실행하세요.", log_path=log_path)
    return _store_products(built, final)


def _store_products(built: Path, final: Path) -> RunnerProducts:
    """Products를 `<key>.partial`에 복사한 뒤 final로 rename한다. 끊긴 복사가 완성 캐시로 보이지 않게 하려는 것이다."""
    partial = final.with_name(final.name + ".partial")
    _remove_tree(partial)
    _remove_tree(final)  # .xctestrun이 없는 폴더는 이전 실행의 잔재다.
    shutil.copytree(built, partial, symlinks=True)
    partial.rename(final)
    return RunnerProducts(_single_xctestrun(final))


def status_proof(proof_key: str, target_bundle: str, nonce: str) -> str:
    """실행기가 handshake(status)에 실어 보내는 proof: hex(HMAC-SHA256(proof_key, 접두사 + 앱 + ":" + nonce)).

    왜 Bearer 토큰이 아닌 별도 proof 키인가: proof 키는 xcodebuild 환경으로만 넘기고 HTTP로는 절대 보내지
    않으므로 진짜 실행기만 맞는 proof를 만들 수 있다. 왜 nonce인가: 매번 새 nonce를 쓰므로 예전에 받아 둔
    proof를 재사용하는 가짜 실행기를 막는다.
    """
    message = f"{STATUS_PROOF_PREFIX}{target_bundle}:{nonce}".encode("utf-8")
    return hmac.new(proof_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def safe_runner_text(value: object) -> str:
    """실행기가 보낸 문자열을 사용자 안내문에 넣을 수 있게 다듬는다.

    글자·숫자(한글 포함)와 일부 문장 부호만 남기고 나머지(제어 문자·ANSI escape 등)는 '?'로 바꾼 뒤 120자로
    자른다. 왜: 실행기 쪽 문자열이 터미널 화면을 조작하거나 안내문을 도배하지 못하게 하려는 것이다.
    """
    text = "".join(character if character.isalnum() or character in _RUNNER_TEXT_PUNCTUATION else "?"
                   for character in str(value)[:_RUNNER_TEXT_LIMIT + 1])
    return text if len(text) <= _RUNNER_TEXT_LIMIT else text[:_RUNNER_TEXT_LIMIT] + "…"


def free_loopback_port() -> int:
    """127.0.0.1에서 지금 비어 있는 TCP 포트 번호를 돌려준다.

    주의: 소켓을 닫은 뒤 실행기가 바인드하기 전에 다른 프로그램이 가져갈 수 있다. 그때는 세션 시작이
    runner_start_failed로 실패하므로 새 포트로 다시 시도하면 된다.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _read_limited(stream: BinaryIO) -> bytes:
    """응답 본문을 최대 32 MiB까지 읽는다. 넘으면 runner_response_too_large."""
    raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SimpleError("runner_response_too_large", "실행기 응답이 32 MiB를 넘었습니다.",
                          "실행기와 도구 버전이 같은지 확인하고, 계속되면 실행기를 다시 빌드하세요.")
    return raw


def _invalid_response(action: str, detail: str) -> SimpleError:
    """응답 형식 오류(runner_response_invalid)를 만든다."""
    return SimpleError("runner_response_invalid", f"실행기의 '{action}' 응답을 해석하지 못했습니다: {detail}",
                       "실행기와 도구 버전이 같은지 확인하고, 계속되면 실행기를 다시 빌드하세요.")


def _decode_response(raw: bytes, action: str) -> dict[str, Any]:
    """응답 본문을 JSON 객체로 해석한다. JSON이 아니거나 객체가 아니면 runner_response_invalid."""
    try:
        document = json.loads(raw)
    except ValueError as error:  # UnicodeDecodeError도 ValueError의 하위 클래스다.
        raise _invalid_response(action, str(error)) from error
    if not isinstance(document, dict):
        raise _invalid_response(action, "JSON 객체가 아닙니다.")
    return document


class RunnerClient:
    """실행기 HTTP 명령 API(`POST http://127.0.0.1:<port>/command`) 클라이언트.

    명령에는 세션 토큰을 Bearer로 싣는다. 시작 handshake(status)만 토큰 없이 보낸다(`handshake` 참고).
    """

    def __init__(self, port: int, *, token: str, timeout: float = 30.0):
        """port: 실행기 포트. token: 세션 토큰(로그·오류에 넣지 않는다).
        timeout: 명령 하나의 기본 응답 대기 시간(초). 긴 제스처는 duration에 맞춰 늘린다."""
        self.port = port
        self._url = f"http://127.0.0.1:{port}/command"
        self._token = token
        self._timeout = timeout

    def command(self, action: str, **payload: Any) -> dict[str, Any]:
        """명령 하나를 토큰과 함께 보내고 응답 사전을 돌려준다. ok가 true가 아니면 runner_command_failed."""
        raw = self._post({**payload, "action": action}, action, is_authenticated=True,
                         timeout=self._timeout_for(payload))
        return self._checked(action, raw)

    def handshake(self, nonce: str) -> dict[str, Any]:
        """토큰 없이 status와 nonce만 보내 실행기의 proof를 받는다.

        왜 토큰 없이: 아직 진짜 실행기인지 모르는 상대(포트를 가로챈 프로세스일 수 있다)에게 토큰을 넘기지 않기
        위해서다. proof 확인은 호출자(RunnerSession)가 한다. ok가 true가 아니면 runner_command_failed.
        """
        raw = self._post({"action": "status", "nonce": nonce}, "status", is_authenticated=False,
                         timeout=self._timeout)
        return self._checked("status", raw)

    def _checked(self, action: str, raw: bytes) -> dict[str, Any]:
        """응답을 해석하고 ok를 확인한다. 실행기가 보낸 error는 다듬어서(safe_runner_text) 안내문에 넣는다."""
        document = _decode_response(raw, action)
        if document.get("ok") is not True:
            reason = safe_runner_text(document.get("error", "이유 없음"))
            raise SimpleError("runner_command_failed", f"실행기가 '{action}' 명령을 처리하지 못했습니다: {reason}",
                              "대상 앱이 실행 중인지, 좌표·입력 값이 올바른지 확인한 뒤 다시 시도하세요. "
                              "계속되면 실행기 세션 로그를 확인하세요.")
        return document

    def _timeout_for(self, payload: dict[str, Any]) -> float:
        """duration이 있는 명령(long_press·swipe)은 duration+10초 이상 기다린다.

        왜: 실행기는 최대 60초 제스처를 허용하는데, 기본 제한 시간(30초)으로는 응답 전에 끊길 수 있다.
        """
        duration = payload.get("duration")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration):
            return max(self._timeout, float(duration) + _GESTURE_TIMEOUT_MARGIN)
        return self._timeout

    def _post(self, body: dict[str, Any], action: str, *, is_authenticated: bool, timeout: float) -> bytes:
        """본문을 POST하고 응답 바이트를 돌려준다. 4xx/5xx도 실행기가 JSON 오류를 담아 보내므로 본문을 읽는다."""
        headers = {"Content-Type": "application/json"}
        if is_authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self._url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            response = _OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise self._unreachable(action, error) from error
        try:
            with response:
                return _read_limited(response)
        except (OSError, http.client.HTTPException) as error:
            raise self._unreachable(action, error) from error

    def _unreachable(self, action: str, error: Exception) -> SimpleError:
        """연결 실패·시간 초과를 runner_unreachable로 바꾼다.

        error 문구는 safe_runner_text로 다듬는다. 왜: http.client.BadStatusLine 등은 상대가 보낸 바이트(최대 64 KiB,
        제어 문자 포함)를 그대로 담아, 포트를 가로챈 프로세스가 안내문을 조작할 수 있기 때문이다.
        """
        return SimpleError("runner_unreachable",
                           f"실행기(127.0.0.1:{self.port})에 '{action}' 명령을 보내지 못했습니다: {safe_runner_text(error)}",
                           "실행기 세션이 살아 있는지 확인하고, 세션 로그를 본 뒤 다시 실행하세요.")

    def hierarchy(self) -> Element:
        """대상 앱의 현재 화면 계층을 받아 Element 트리로 돌려준다. 형식이 틀리면 hierarchy_invalid."""
        return parse_hierarchy(self.command("hierarchy").get("root"))

    def tap_point(self, x: float, y: float) -> None:
        """화면 좌표계 포인트 (x, y)를 탭한다(hierarchy frame과 같은 좌표계, 정규화 좌표가 아니다)."""
        self.command("tap", x=x, y=y)

    def type_text(self, text: str) -> None:
        """포커스된 입력 칸에 text를 입력한다. 256자를 넘으면 보내지 않고 runner_text_too_long."""
        if len(text) > MAX_TEXT_LENGTH:
            raise SimpleError("runner_text_too_long",
                              f"한 번에 입력할 수 있는 글자는 {MAX_TEXT_LENGTH}자까지입니다(요청 {len(text)}자).",
                              f"입력을 {MAX_TEXT_LENGTH}자 이하로 나눠 여러 번 보내세요.")
        self.command("type", text=text)

    def launch(self, arguments: list[str], environment: dict[str, str]) -> None:
        """대상 앱을 실행 인자·환경 변수와 함께 (다시) 실행한다."""
        self.command("launch", arguments=list(arguments), environment=dict(environment))

    def terminate(self) -> None:
        """대상 앱을 종료한다."""
        self.command("terminate")


class RunnerSession:
    """실행기 xcodebuild 세션의 수명을 관리하는 context manager. `with`가 RunnerClient를 돌려준다.

    xcodebuild의 DerivedData·결과 묶음은 log_path 옆의 0700 세션 폴더(`runner-session-<uuid>`)에 둔다.
    왜: 기본 위치(~/Library/Developer/Xcode/DerivedData)의 결과 묶음에는 세션 비밀 값과 입력한 글자가
    0644로 남기 때문이다. stop이 성공하면 세션 폴더를 지운다.
    왜 run_command를 쓰지 않나: 세션은 몇 시간까지 이어지는 장기 프로세스라 끝날 때까지 기다릴 수 없다.
    """

    def __init__(self, products: RunnerProducts, *, udid: str, target_bundle: str, port: int,
                 log_path: Path, start_timeout: float = 180.0):
        """products: 빌드된 실행기. udid: 대상 Simulator. target_bundle: 구동할 앱의 bundle id.
        port: 실행기 포트. log_path: xcodebuild 출력 로그. start_timeout: handshake를 기다리는 시간(초)."""
        self._products = products
        self._udid = udid
        self._target_bundle = target_bundle
        self._port = port
        self._log_path = Path(log_path)
        self._start_timeout = start_timeout
        self._last_probe_error = "아직 응답 없음"
        self.session_dir: Path | None = None

    def session_argv(self) -> list[str]:
        """세션 xcodebuild argv. 경로는 인자 하나로 넘겨 공백·한글이 깨지지 않는다.

        DerivedData와 결과 묶음은 세션 폴더로 보낸다(사용자 DerivedData에 비밀 값이 남지 않게).
        """
        return ["xcodebuild", "test-without-building", "-xctestrun", str(self._products.xctestrun),
                "-destination", f"id={self._udid}", f"-only-testing:{SESSION_TEST}",
                "-derivedDataPath", str(self.session_dir / "dd"),
                "-resultBundlePath", str(self.session_dir / "result.xcresult")]

    def _environment(self) -> dict[str, str]:
        """최소 사용자 환경 + 기본 환경 + TEST_RUNNER_ 변수. xcodebuild가 접두사를 떼고 테스트에 넘긴다."""
        environment = {key: os.environ[key] for key in _INHERITED_ENVIRONMENT if key in os.environ}
        environment.update(build_default_environment())
        environment["TEST_RUNNER_REPROOF_RUNNER_PORT"] = str(self._port)
        environment["TEST_RUNNER_REPROOF_TARGET_BUNDLE"] = self._target_bundle
        environment["TEST_RUNNER_REPROOF_RUNNER_TOKEN"] = self._token
        environment["TEST_RUNNER_REPROOF_RUNNER_PROOF_KEY"] = self._proof_key
        return environment

    def __enter__(self) -> RunnerClient:
        """새 비밀 값으로 xcodebuild를 새 세션 그룹으로 띄우고 handshake가 검증될 때까지 기다린다.

        실패하면 그룹을 정리하고 다시 던진다(세션 폴더는 진단용으로 남긴다).
        """
        self._new_secrets()
        self._log = self._prepare_files()
        self._process = self._spawn()
        try:
            self._wait_until_ready()
        except BaseException:
            self._cleanup()
            raise
        return self.client

    def __exit__(self, exc_type, exc, traceback) -> None:
        """stop을 보내고, 성공하면 xcodebuild가 스스로 끝나기를 최대 10초 기다린 뒤 프로세스 그룹을 정리한다.

        stop이 성공했으면 세션 폴더를 지우고, 실패했으면 진단용으로 남긴다. 본문이 예외로 끝났으면 그 예외를
        그대로 전파하고(정리 문제는 로그에만 남긴다), 아니면 정리 문제를 SimpleError로 알린다.
        왜 그룹 정리를 finally에 두나: stop·대기 중 Ctrl-C(KeyboardInterrupt)나 로그 쓰기 오류가 나도 반드시
        정리해야 한다. start_new_session=True라 터미널 SIGINT가 xcodebuild에 닿지 않아, 정리를 건너뛰면 Simulator와
        포트를 쥔 세션이 최대 6시간 남는다.
        """
        problem = None
        try:
            is_stopped = False
            try:
                is_stopped = self._send_stop()
                if is_stopped:
                    self._wait_for_exit()
            finally:
                self._stop_process_group()
            problem = self._settle_session_dir(is_stopped)
        finally:
            self._log.close()
        if problem is not None and exc_type is None:
            raise problem

    def _new_secrets(self) -> None:
        """세션마다 토큰·proof 키를 새로 만들고 토큰을 쓰는 클라이언트를 준비한다(이전 세션 값으로 조작·사칭 방지)."""
        self._token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._proof_key = secrets.token_urlsafe(_TOKEN_BYTES)
        self.client = RunnerClient(self._port, token=self._token)

    def _prepare_files(self) -> BinaryIO:
        """세션 폴더(0700)와 로그 파일(0600, 추가 모드)을 만든다. 만들 수 없으면 runner_files_unwritable.

        왜 권한을 좁히나: 세션 폴더의 결과 묶음과 로그에 비밀 값·입력한 글자가 남을 수 있어 소유자만 읽게 한다.
        """
        folder = self._log_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.session_dir = folder / f"{_SESSION_DIR_PREFIX}{uuid.uuid4().hex}"
            self.session_dir.mkdir(mode=0o700)
            os.chmod(self.session_dir, 0o700)  # umask와 상관없이 0700으로 맞춘다.
            descriptor = os.open(self._log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
        except OSError as error:
            raise SimpleError("runner_files_unwritable", f"실행기 로그·세션 폴더를 만들지 못했습니다({folder}): {error}",
                              "출력 폴더가 쓰기 가능한지, 같은 이름의 파일이 폴더 자리를 차지하고 있지 않은지 "
                              "확인한 뒤 다시 실행하세요.") from error
        return open(descriptor, "ab")

    def _spawn(self) -> subprocess.Popen:
        """xcodebuild 세션을 띄운다. stdout/stderr는 로그로 보낸다. 실행 자체가 안 되면 runner_start_failed."""
        try:
            return subprocess.Popen(self.session_argv(), stdin=subprocess.DEVNULL, stdout=self._log,
                                    stderr=subprocess.STDOUT, env=self._environment(), start_new_session=True)
        except OSError as error:
            self._log.close()
            raise self._start_failed(f"xcodebuild를 실행하지 못했습니다: {error}") from error

    def _wait_until_ready(self) -> None:
        """새 nonce로 handshake를 0.5초 간격으로 시도한다. 응답이 오면 proof·대상 앱을 확인하고 끝낸다.

        xcodebuild가 먼저 끝나거나 시간이 지나면 runner_start_failed. 토큰은 검증이 끝난 뒤의 명령에만 실린다.
        """
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            exit_code = self._process.poll()
            if exit_code is not None:
                raise self._start_failed(f"xcodebuild가 실행기 준비 전에 끝났습니다(종료 코드 {exit_code}).")
            nonce = secrets.token_hex(_NONCE_BYTES)
            status = self._probe_status(nonce)
            if status is not None:
                self._check_target(status, nonce)
                return
            time.sleep(_POLL_INTERVAL)
        raise self._start_failed(f"{self._start_timeout:.0f}초 안에 실행기가 응답하지 않았습니다"
                                 f"(마지막 오류: {self._last_probe_error}).")

    def _probe_status(self, nonce: str) -> dict[str, Any] | None:
        """handshake를 한 번 시도한다. 아직 준비 전이면 None을 돌려주고 원인은 시간 초과 안내에 쓰도록 남긴다.

        왜 None인가: 시작 중에는 서버가 아직 없어 연결이 거부되는 것이 정상 경로다.
        """
        try:
            return RunnerClient(self._port, token=self._token, timeout=_PROBE_TIMEOUT).handshake(nonce)
        except SimpleError as error:
            self._last_probe_error = error.cause
            return None

    def _check_target(self, status: dict[str, Any], nonce: str) -> None:
        """응답한 쪽이 이 세션의 실행기이고 요청한 앱을 구동하는지 확인한다.

        proof(proof 키·nonce로 만든 HMAC, status_proof 참고)가 맞아야 한다. 왜: 포트를 먼저 가로챈 다른
        프로세스가 targetBundle만 흉내 내 토큰과 명령(입력할 글자 등)을 받아 가지 못하게 하려는 것이다.
        비교는 상수 시간으로 한다.
        """
        if status.get("targetBundle") != self._target_bundle:
            shown = safe_runner_text(status.get("targetBundle"))
            raise self._start_failed(f"포트 {self._port}의 실행기가 다른 앱({shown})을 구동하고 있습니다.")
        proof, expected = status.get("proof"), status_proof(self._proof_key, self._target_bundle, nonce)
        if not isinstance(proof, str) or not hmac.compare_digest(expected.encode("utf-8"), proof.encode("utf-8")):
            raise self._start_failed(f"포트 {self._port}에서 응답한 프로그램이 이 세션의 실행기임을 증명하지 못했습니다"
                                     "(status proof 불일치).")

    def _kept_session_dir_notice(self) -> str:
        """진단용으로 남긴 세션 폴더 안내. 비밀 값이 있을 수 있으니 확인 후 지우라고 알린다."""
        return (f"진단용으로 남긴 세션 폴더({self.session_dir})의 결과 묶음에는 세션 비밀 값(토큰 등)과 입력한 글자가 "
                "들어 있을 수 있으니 확인한 뒤 지우세요.")

    def _start_failed(self, detail: str) -> SimpleError:
        """runner_start_failed 오류를 만든다. 로그 경로와 남긴 세션 폴더 안내를 함께 싣는다."""
        return SimpleError("runner_start_failed", f"실행기 세션을 시작하지 못했습니다: {detail}",
                           f"로그를 확인하세요. Simulator({self._udid})가 켜져 있고 대상 앱({self._target_bundle})이 "
                           "설치돼 있는지, 같은 포트를 쓰는 다른 프로그램이 없는지 확인한 뒤 다시 실행하세요. "
                           + self._kept_session_dir_notice(), log_path=self._log_path)

    def _send_stop(self) -> bool:
        """stop을 보내 testSession을 끝낸다. 성공하면 True, 보내지 못하면 로그에 남기고 False."""
        try:
            RunnerClient(self._port, token=self._token, timeout=_PROBE_TIMEOUT).command("stop")
        except SimpleError as error:
            self._note(f"stop 명령을 보내지 못해 프로세스 그룹을 강제로 정리합니다: {error.cause}")
            return False
        return True

    def _wait_for_exit(self) -> None:
        """stop 뒤 xcodebuild가 결과 정리를 마치고 스스로 끝나기를 최대 10초 기다린다.

        왜: 바로 SIGTERM을 보내면 BUILD INTERRUPTED로 끝나고 결과 묶음이 반쯤 쓰인 채 남는다. 넘으면 로그에 남긴다.
        """
        try:
            self._process.wait(timeout=_NATURAL_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self._note(f"stop 뒤 {_NATURAL_EXIT_SECONDS}초 안에 xcodebuild가 끝나지 않아 강제로 정리합니다.")

    def _settle_session_dir(self, is_stopped: bool) -> SimpleError | None:
        """stop이 성공했으면 세션 폴더를 지우고, 아니면 남긴다. 알릴 문제가 있으면 SimpleError를 돌려준다.

        남길 때는 로그에도 안내를 적는다. 왜: 본문이 예외로 끝나면 이 SimpleError는 원래 예외를 가리지 않도록
        버려지므로, 비밀 값이 남은 폴더를 알릴 곳이 로그뿐이다.
        """
        if not is_stopped:
            self._note(self._kept_session_dir_notice())
            return SimpleError("runner_stop_failed", "실행기 세션을 정상적으로 끝내지 못했습니다(stop 실패). "
                               "프로세스는 강제로 정리했습니다.", "로그를 확인하세요. " + self._kept_session_dir_notice(),
                               log_path=self._log_path)
        try:
            shutil.rmtree(self.session_dir)
        except OSError as error:
            self._note(f"세션 폴더를 지우지 못했습니다: {error}")
            return SimpleError("runner_cleanup_failed", f"세션 폴더({self.session_dir})를 지우지 못했습니다: {error}",
                               "세션 비밀 값과 입력한 글자가 남아 있을 수 있으니 이 폴더를 직접 지우세요.",
                               log_path=self._log_path)
        return None

    def _note(self, message: str) -> None:
        """세션 로그에 reproof 쪽 진단 한 줄을 덧붙인다. 비밀 값은 넣지 않는다."""
        self._log.write(f"[reproof] {message}\n".encode("utf-8"))
        self._log.flush()

    def _cleanup(self) -> None:
        """프로세스 그룹을 정리하고 로그 파일을 닫는다(시작 실패 경로)."""
        try:
            self._stop_process_group()
        finally:
            self._log.close()

    def _stop_process_group(self) -> None:
        """SIGTERM → 최대 2초 대기 → SIGKILL. 부모가 끝나도 그룹의 자식이 남을 수 있어 SIGKILL은 항상 보낸다.

        SIGKILL은 finally에 둔다: 대기 중 Ctrl-C나 로그 쓰기 오류가 나도 그룹이 남지 않게 하려는 것이다.
        """
        try:
            self._signal_group(signal.SIGTERM)
            try:
                self._process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._note(f"xcodebuild가 {_TERMINATE_GRACE_SECONDS}초 안에 끝나지 않아 SIGKILL로 정리합니다.")
        finally:
            self._signal_group(signal.SIGKILL)
            self._process.wait()

    def _signal_group(self, signal_number: int) -> None:
        """세션 프로세스 그룹에 신호를 보낸다. EPERM은 signal_process_group이 최대 2초 다시 시도한다.

        그래도 EPERM이면 로그에만 남기고 돌아간다. 왜: 여기서 PermissionError를 던지면 SIGKILL·회수·세션 폴더 정리를
        건너뛰고, 본문의 원래 오류까지 가린다. 남았을 수 있는 프로세스는 로그 안내로 알린다.
        """
        try:
            signal_process_group(self._process, signal_number)
        except PermissionError as error:
            name = signal.Signals(signal_number).name
            self._note(f"프로세스 그룹에 {name}를 보내지 못했습니다(EPERM, 2초 재시도 후): {error}. 정리를 이어 갑니다. "
                       "남은 xcodebuild가 있으면 `pgrep -fl SimpleRunner`로 확인하세요.")
