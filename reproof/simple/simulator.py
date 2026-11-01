"""reproof 전용 Simulator 관리. 사용자의 기존 Simulator는 조회만 하고 절대 바꾸지 않는다."""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import SimulatorSettings
from .errors import SimpleError, ToolFailure
from .process import ToolRunner

# 이 툴이 소유한 Simulator 이름 접두사. 이 접두사가 없는 기기는 변경하지 않는다.
OWNED_PREFIX = "reproof-"
# simctl 일반 명령 제한 시간(초).
_COMMAND_TIMEOUT = 120
# 부팅 완료 대기 제한 시간(초). 첫 부팅은 데이터 마이그레이션 때문에 오래 걸린다.
_BOOT_WAIT_TIMEOUT = 300
# 이미 꺼진 기기를 shutdown할 때 simctl이 내는 문구. 이 경우만 정상으로 본다.
_ALREADY_SHUTDOWN = "current state: Shutdown"
_IOS_RUNTIME_MARKER = "SimRuntime.iOS-"
# 기기 이름에 붙이는 소스 경로 해시 길이(16진 글자 수).
_SOURCE_HASH_LENGTH = 6


def owned_device_name(project_name: str, source_root: Path) -> str:
    """전용 기기 이름 `reproof-<project_name>-<sha256(소스 루트 실경로) 앞 6자>`를 돌려준다.

    왜 경로 해시인가: 이름(slug)만 쓰면 `MyApp`/`my-app`, 같은 앱의 두 클론, 다른 작업 공간의 실행이 같은 기기를
    초기화해 서로의 앱 데이터를 지운다. 실경로(realpath)를 써서 링크로 들어온 같은 폴더는 같은 기기를 쓰게 한다.
    """
    digest = hashlib.sha256(os.path.realpath(source_root).encode("utf-8", "surrogateescape")).hexdigest()
    return f"{OWNED_PREFIX}{project_name}-{digest[:_SOURCE_HASH_LENGTH]}"


@dataclass(frozen=True)
class Runtime:
    """Simulator 런타임 한 개. identifier는 simctl 식별자, version은 "27.0" 형태 문자열."""

    identifier: str
    name: str
    version: str
    is_available: bool


def _simctl_json(runner: ToolRunner, subcommand: str) -> dict[str, Any]:
    """`xcrun simctl list <subcommand> --json`을 실행해 파싱한다. 형식이 깨졌으면 SimpleError."""
    output = runner.run(["xcrun", "simctl", "list", subcommand, "--json"],
                        cwd=Path.cwd(), timeout=_COMMAND_TIMEOUT)
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as error:
        raise SimpleError("simulator_output_invalid", f"simctl {subcommand} 출력을 해석하지 못했습니다: {error}",
                          "Xcode가 정상 설치됐는지 `xcrun simctl list`로 확인한 뒤 다시 실행하세요.") from error
    if not isinstance(parsed, dict):
        raise SimpleError("simulator_output_invalid", f"simctl {subcommand} 출력이 객체가 아닙니다.",
                          "Xcode가 정상 설치됐는지 `xcrun simctl list`로 확인한 뒤 다시 실행하세요.")
    return parsed


def _unexpected_output(subcommand: str, error: Exception) -> SimpleError:
    """simctl JSON 구조가 예상과 다를 때 KeyError 대신 보여줄 오류를 만든다(Xcode 버전 차이 대비)."""
    return SimpleError("simulator_output_invalid", f"simctl {subcommand} 출력 구조가 예상과 다릅니다: {error!r}",
                       "Xcode가 정상 설치됐는지 `xcrun simctl list`로 확인한 뒤 다시 실행하세요.")


def list_runtimes(runner: ToolRunner) -> tuple[Runtime, ...]:
    """설치된 iOS 런타임만 모아 돌려준다(watchOS 등은 제외). 사용 불가 항목도 포함하고 표시만 한다."""
    entries = _simctl_json(runner, "runtimes").get("runtimes", [])
    try:
        return tuple(
            Runtime(item["identifier"], item["name"], str(item.get("version", "")), bool(item.get("isAvailable")))
            for item in entries if _IOS_RUNTIME_MARKER in item.get("identifier", ""))
    except (KeyError, TypeError, AttributeError) as error:
        raise _unexpected_output("runtimes", error) from error


def _version_key(runtime: Runtime) -> tuple[int, ...]:
    """"27.0.1" 같은 버전을 정수 튜플로 바꿔 숫자 순서로 비교하게 한다(문자열 비교는 "9" > "27" 오류)."""
    return tuple(int(part) for part in runtime.version.split(".") if part.isdigit())


def pick_runtime(runtimes: Sequence[Runtime], requested: str) -> Runtime:
    """요청에 맞는 사용 가능한 iOS 런타임을 고른다.

    requested: "latest"(가장 높은 버전) 또는 "iOS 27.0" 같은 정확한 이름.
    반환: 선택된 Runtime. 없으면 ios_runtime_missing, 이름이 없으면 ios_runtime_not_found.
    """
    available = [runtime for runtime in runtimes if runtime.is_available]
    if not available:
        raise SimpleError("ios_runtime_missing", "사용 가능한 iOS Simulator 런타임이 없습니다.",
                          "`xcodebuild -downloadPlatform iOS`로 iOS 런타임을 설치한 뒤 다시 실행하세요.")
    if requested == "latest":
        return max(available, key=_version_key)
    for runtime in available:
        if runtime.name == requested:
            return runtime
    names = ", ".join(runtime.name for runtime in available)
    raise SimpleError("ios_runtime_not_found", f"요청한 런타임 '{requested}'이(가) 없습니다. 사용 가능: {names}",
                      "reproof.toml의 [simulator] runtime을 위 이름 중 하나나 'latest'로 고치세요.")


def launch_locale_settings(settings: SimulatorSettings) -> tuple[list[str], dict[str, str]]:
    """앱 실행 시 넘길 언어·지역 인자와 시간대 환경 변수를 만든다.

    왜: 스크린샷이 실행 환경과 무관하게 같게 나오도록 설정 값을 항상 명시해 전달한다.
    반환: (launch 인자 목록, 환경 변수 사전).
    """
    arguments = ["-AppleLanguages", f"({settings.language})", "-AppleLocale", settings.locale]
    return arguments, {"TZ": settings.timezone}


class SimulatorManager:
    """reproof 전용 Simulator의 생성·초기화·표시 고정·앱 설치를 맡는다."""

    def __init__(self, runner: ToolRunner):
        """명령 실행기를 받는다. 테스트에서는 FakeToolRunner를 넣는다."""
        self._runner = runner

    def _simctl(self, args: list[str], timeout: int = _COMMAND_TIMEOUT) -> str:
        """`xcrun simctl <args>`를 실행하고 stdout을 돌려준다."""
        return self._runner.run(["xcrun", "simctl", *args], cwd=Path.cwd(), timeout=timeout)

    def _find_device_type(self, name: str) -> str:
        """기기 이름으로 device type 식별자를 찾는다. 없으면 사용 가능한 iPhone 이름을 알린다."""
        types = _simctl_json(self._runner, "devicetypes").get("devicetypes", [])
        try:
            for item in types:
                if item["name"] == name:
                    return item["identifier"]
            iphones = ", ".join(item["name"] for item in types if item["name"].startswith("iPhone"))
        except (KeyError, TypeError, AttributeError) as error:
            raise _unexpected_output("devicetypes", error) from error
        raise SimpleError("device_type_not_found", f"기기 종류 '{name}'을(를) 찾지 못했습니다. 사용 가능한 iPhone: {iphones}",
                          "reproof.toml의 [simulator] device_type을 위 이름 중 하나로 고치세요.")

    def _find_owned_device(self, name: str, type_id: str, runtime_id: str) -> str | None:
        """이름·종류·런타임이 모두 같은 기존 기기의 udid를 찾는다. 없으면 None."""
        devices = _simctl_json(self._runner, "devices").get("devices", {})
        for item in devices.get(runtime_id, []):
            is_match = item.get("name") == name and item.get("deviceTypeIdentifier") == type_id
            if is_match and item.get("isAvailable") is True:  # 사용 불가 기기는 재사용하지 않고 새로 만든다.
                return item["udid"]
        return None

    def ensure_device(self, name: str, settings: SimulatorSettings) -> str:
        """`reproof-` 접두사 기기 name(보통 owned_device_name 결과)을 재사용하거나 새로 만들어 udid를 돌려준다.

        왜 새로 만들기만 하나: 종류·런타임이 다른 기존 기기를 지우면 사용자 데이터를 잃을 수 있어
        옛 기기는 그대로 둔다. 주의: 접두사가 없는 이름이면 아무것도 조회·생성하지 않고 simulator_not_owned.
        """
        if not name.startswith(OWNED_PREFIX):
            raise SimpleError("simulator_not_owned", f"Simulator 이름 '{name}'은(는) reproof 전용 이름이 아닙니다.",
                              f"'{OWNED_PREFIX}'로 시작하는 이름(owned_device_name)만 쓰세요.")
        runtime = pick_runtime(list_runtimes(self._runner), settings.runtime)
        type_id = self._find_device_type(settings.device_type)
        existing = self._find_owned_device(name, type_id, runtime.identifier)
        if existing is not None:
            return existing
        return self._simctl(["create", name, type_id, runtime.identifier]).strip()

    def _require_owned(self, udid: str) -> str:
        """udid가 reproof- 접두사 기기인지 확인하고 현재 state를 돌려준다. 아니거나 모르는 기기면 simulator_not_owned."""
        devices = _simctl_json(self._runner, "devices").get("devices", {})
        for group in devices.values():
            for item in group:
                if item.get("udid") == udid and item.get("name", "").startswith(OWNED_PREFIX):
                    return str(item.get("state", ""))
        raise SimpleError("simulator_not_owned", f"Simulator {udid}은(는) reproof 전용 기기가 아니어서 변경할 수 없습니다.",
                          "reproof가 만든 'reproof-' 기기만 사용합니다. 설정을 다시 확인하거나 ensure_device로 전용 기기를 만드세요.")

    def _shutdown_if_running(self, udid: str, state: str) -> None:
        """Shutdown 상태면 건너뛰고, 아니면 끈다.

        왜 state를 먼저 보나: simctl 문구는 Xcode 버전마다 바뀔 수 있어, 새로 만든 기기(항상 Shutdown)는 문구에
        기대지 않고 건너뛴다. 출력 꼬리(HostToolRunner가 임시 로그로 받는다)의 문구 매칭은 보조 수단이다.
        """
        if state == "Shutdown":
            return
        try:
            self._simctl(["shutdown", udid])
        except ToolFailure as failure:
            if _ALREADY_SHUTDOWN not in failure.output_tail:
                raise

    def reset(self, udid: str) -> None:
        """shutdown, erase, boot, 부팅 완료 대기 순서로 깨끗한 상태를 만든다. 매 재생을 같은 상태에서 시작하려는 것이다."""
        state = self._require_owned(udid)
        self._shutdown_if_running(udid, state)
        self._simctl(["erase", udid])
        self._simctl(["boot", udid])
        self._simctl(["bootstatus", udid, "-b"], timeout=_BOOT_WAIT_TIMEOUT)

    def apply_presentation(self, udid: str) -> None:
        """상태 바(9:41, 배터리 100%, 신호 고정)·라이트 모드·글자 크기를 고정한다. 스크린샷 차이를 줄이려는 것이다."""
        self._require_owned(udid)
        self._simctl(["status_bar", udid, "override", "--time", "9:41", "--batteryState", "charged",
                      "--batteryLevel", "100", "--cellularBars", "4", "--wifiBars", "3"])
        self._simctl(["ui", udid, "appearance", "light"])
        self._simctl(["ui", udid, "content_size", "large"])

    def install_app(self, udid: str, app_path: Path) -> None:
        """빌드된 .app을 설치한다. 경로는 인자 하나로 넘겨 공백·한글이 깨지지 않는다."""
        self._require_owned(udid)
        self._simctl(["install", udid, str(app_path)])

    def uninstall_app(self, udid: str, bundle_id: str) -> None:
        """번들 ID의 앱을 제거한다."""
        self._require_owned(udid)
        self._simctl(["uninstall", udid, bundle_id])
