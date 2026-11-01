"""외부 명령 실행기. 기존 repair.run_command를 감싸 간편 모드용 환경과 오류 형식을 맞춘다."""
import os
import subprocess
from pathlib import Path
from typing import Protocol

from ..repair import CommandError, run_command
from .errors import ToolFailure

# 실패 시 오류에 싣는 로그 끝부분 크기(바이트).
_TAIL_BYTES = 4096
# 도구 출력 상한. xcodebuild 로그가 커질 수 있어 기본값보다 크게 둔다.
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
# xcode-select 조회 결과 캐시. 프로세스당 한 번만 조회한다.
_developer_dir_cache: list[str | None] = []


class ToolRunner(Protocol):
    """외부 명령 실행 계약. 테스트에서는 FakeToolRunner로 바꿔 끼운다."""

    def run(self, argv: list[str], *, cwd: Path, timeout: int, log_path: Path | None = None,
            env_extra: dict[str, str] | None = None) -> str:
        """argv를 cwd에서 실행하고 stdout을 돌려준다. 실패하면 ToolFailure를 낸다."""
        ...


def lookup_developer_dir() -> str | None:
    """`xcode-select -p` 결과를 프로세스당 한 번 조회해 캐시한다.

    반환: 경로 문자열, xcode-select가 없거나 실패하면 None. 왜: CI(Ubuntu)처럼 Xcode가 없는
    환경에서는 DEVELOPER_DIR를 아예 넣지 않아야 하고, 매 호출마다 프로세스를 띄우면 낭비다.
    """
    if not _developer_dir_cache:
        _developer_dir_cache.append(_query_developer_dir())
    return _developer_dir_cache[0]


def _query_developer_dir() -> str | None:
    """xcode-select를 실제로 실행한다. 없거나 실패하거나 빈 출력이면 None."""
    try:
        result = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None  # Xcode가 없는 환경은 정상 경우이므로 DEVELOPER_DIR만 생략한다.
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def build_default_environment() -> dict[str, str]:
    """기본 추가 환경 변수를 만든다.

    DEVELOPER_DIR(있을 때), GIT_TERMINAL_PROMPT=0(인증 입력 대기로 멈추는 것 방지),
    SSH_AUTH_SOCK(있을 때만; 비공개 SPM이 기존 git 인증을 쓰도록 값만 전달하고 읽지는 않는다).
    """
    environment = {"GIT_TERMINAL_PROMPT": "0"}
    developer_dir = lookup_developer_dir()
    if developer_dir:
        environment["DEVELOPER_DIR"] = developer_dir
    if os.environ.get("SSH_AUTH_SOCK"):
        environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return environment


def read_log_tail(log_path: Path | None) -> str:
    """로그 끝 4096바이트를 문자열로 읽는다. 로그가 없거나 읽을 수 없으면 빈 문자열."""
    if log_path is None:
        return ""
    try:
        data = Path(log_path).read_bytes()
    except OSError:
        return ""  # 로그는 보조 정보라 읽기 실패가 원래 오류를 가리면 안 된다.
    return data[-_TAIL_BYTES:].decode("utf-8", "replace")


class HostToolRunner:
    """개발자 Mac에서 실제 명령을 실행하는 ToolRunner 구현."""

    def run(self, argv: list[str], *, cwd: Path, timeout: int, log_path: Path | None = None,
            env_extra: dict[str, str] | None = None) -> str:
        """기본 환경에 호출자의 env_extra를 덮어써 실행한다.

        반환: stdout. 실패·시간 초과 시 ToolFailure(로그 끝부분 포함). 왜: 호출자가 CommandError의
        일반 문구 대신 도구 이름과 로그 위치를 담은 사용자용 오류를 받게 하려는 것이다.
        """
        environment = {**build_default_environment(), **(env_extra or {})}
        try:
            return run_command(argv, str(cwd), timeout=timeout, max_output=_MAX_OUTPUT_BYTES,
                               env_extra=environment, log_path=log_path)
        except CommandError as error:
            raise ToolFailure(Path(argv[0]).name, log_path=log_path,
                              output_tail=read_log_tail(log_path)) from error
