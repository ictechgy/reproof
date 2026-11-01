"""간편 모드 테스트 공용 도구. Xcode나 실제 프로세스 없이 ToolRunner를 대신한다."""
from pathlib import Path


class FakeToolRunner:
    """미리 정한 응답을 돌려주는 ToolRunner 대역.

    왜: 단위 테스트가 xcodebuild 같은 실제 도구에 의존하지 않게 하려는 것이다.
    responses: (argv 앞부분, 출력 문자열 / 던질 예외 / 콜백) 목록. 콜백은 (argv, cwd, env_extra)를 받아 출력 문자열을 돌려준다(파일 생성 등 부수 효과용). 앞부분이 일치하는 첫 응답을 쓴다.
    calls: 호출 기록. 각 항목은 {"argv", "cwd", "env_extra"}.
    주의: 일치하는 응답이 없으면 AssertionError를 내서 예상 밖 호출을 바로 드러낸다.
    """

    def __init__(self, responses):
        """응답 목록을 저장하고 호출 기록을 비운다."""
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, *, cwd, timeout, log_path=None, env_extra=None):
        """호출을 기록하고 일치하는 첫 응답을 돌려주거나 예외를 던진다."""
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "env_extra": env_extra})
        for prefix, response in self.responses:
            if tuple(argv[:len(prefix)]) == tuple(prefix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(list(argv), Path(cwd), env_extra)
                return response
        raise AssertionError(f"일치하는 응답이 없습니다: {argv}")


def sample_config():
    """MixedShop 예제와 같은 유효한 ProjectConfig를 만든다.

    왜: 설정 관련 테스트들이 같은 기준 값을 쓰고, 필요한 필드만 replace로 바꾸게 하려는 것이다.
    반환: 모든 선택 필드가 채워진 ProjectConfig.
    """
    from reproof.simple.config import ProjectConfig, SimulatorSettings

    return ProjectConfig(
        name="mixedshop",
        container="MixedShop.xcodeproj",
        generator="xcodegen",
        generator_spec="project.yml",
        scheme="MixedShop",
        app_target="MixedShop",
        bundle_id="io.reproof.example.mixedshop",
        test_scheme="MixedShopTests",
        simulator=SimulatorSettings(device_type="iPhone 16", runtime="latest"),
        launch_arguments=("-AppleLanguages", "(ko)"),
        launch_environment=(("API_TOKEN", "env:REPROOF_APP_API_TOKEN"),),
    )


def spawn_zombie_group(timeout=5.0):
    """리더가 끝났지만 아직 회수(wait)되지 않은 프로세스 그룹을 만들고 그 Popen을 돌려준다.

    왜: macOS는 좀비만 남은 그룹에 killpg하면 EPERM(PermissionError)을 내므로, 정리 코드가 이 경우를 견디는지
    실제 커널 동작으로 확인하려는 것이다. 신호 0으로 그룹을 살펴 PermissionError가 나오면 좀비 상태다
    (Python 3.11의 macOS 빌드에는 os.waitid가 없어 이 방법을 쓴다).
    주의: 호출자가 끝에 wait()로 회수해야 한다. timeout초 안에 좀비가 되지 않으면 AssertionError.
    """
    import os
    import subprocess
    import time

    process = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except PermissionError:
            return process
        time.sleep(0.01)
    process.wait()
    raise AssertionError("테스트용 프로세스가 제한 시간 안에 좀비 상태가 되지 않았습니다.")
