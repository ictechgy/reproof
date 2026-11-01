"""외부 명령 실행기. 기존 repair.run_command를 감싸 간편 모드용 환경과 오류 형식을 맞춘다."""
import contextlib
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from ..repair import CommandError, run_command
from .errors import ToolFailure
from .redaction import mask_secrets
from .workcopy import replacing_file

# 실패 시 오류에 싣는 로그 끝부분 크기(바이트).
_TAIL_BYTES = 4096
# 줄바꿈이 없는 잘린 창에서 첫 조각이 끝나는 글자(공백·탭).
_FIRST_SEPARATOR = re.compile(rb"[ \t]")
# run_command가 로그에서 stdout과 stderr 사이에 넣는 구분 줄.
_STDERR_MARKER = "\n--- stderr ---\n"
# 안내문에 싣는 마지막 출력 줄 수와 최대 글자 수.
_SUMMARY_LINES = 3
_SUMMARY_LIMIT = 300
# 터미널 색·커서를 바꾸는 ANSI escape 순서.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
# repair.run_command의 실패 문구. 실패 방식을 구분해 안내하려고 읽는다.
_EXIT_CODE_MESSAGE = re.compile(r"Command failed with exit code (-?[0-9]+)")
_TIMEOUT_MESSAGE = "Command time budget exhausted"
_NOT_STARTED_MESSAGE = "Command could not start"
_OUTPUT_LIMIT_MESSAGE = "Command output limit exceeded"
# 도구 출력 상한. xcodebuild 로그가 커질 수 있어 기본값보다 크게 둔다.
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
# 있으면 값을 읽지 않고 그대로 넘기는 사용자 환경 변수. 회사 프록시·사내 CA·git SSH 설정 뒤에서도 SPM·git이
# 개발자 터미널과 같게 동작하게 하려는 것이다(run_command는 HOME·PATH 등 최소 환경만 물려준다).
_PASSTHROUGH_ENVIRONMENT = ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                            "GIT_SSH_COMMAND", "SSL_CERT_FILE", "GIT_SSL_CAINFO")
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
    SSH_AUTH_SOCK(있을 때만; 비공개 SPM이 기존 git 인증을 쓰도록 값만 전달하고 읽지는 않는다),
    프록시·CA·GIT_SSH_COMMAND 변수(_PASSTHROUGH_ENVIRONMENT, 있을 때만; 값은 로그·오류에 쓰지 않는다).
    """
    environment = {"GIT_TERMINAL_PROMPT": "0"}
    developer_dir = lookup_developer_dir()
    if developer_dir:
        environment["DEVELOPER_DIR"] = developer_dir
    for name in ("SSH_AUTH_SOCK", *_PASSTHROUGH_ENVIRONMENT):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    return environment


def read_log_tail(log_path: Path | None) -> str:
    """로그 끝 4096바이트를 읽는다. 창이 파일 중간에서 시작하면 잘린 첫 조각은 버린다(_drop_partial_start). 없거나 못
    읽으면 빈 문자열.

    왜 조각을 버리나: 'https:' 바로 뒤에서 잘린 '//user:pw@host' 같은 조각은 가리기 규칙이 알아보지 못해, 그대로
    안내문에 실릴 수 있다. 파일 끝에서 seek해 읽어 큰 로그도 전부 메모리에 올리지 않는다.
    """
    if log_path is None:
        return ""
    try:
        with open(log_path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - _TAIL_BYTES))
            data = stream.read(_TAIL_BYTES)
    except OSError:
        return ""  # 로그는 보조 정보라 읽기 실패가 원래 오류를 가리면 안 된다.
    return (_drop_partial_start(data) if size > _TAIL_BYTES else data).decode("utf-8", "replace")


def _drop_partial_start(data: bytes) -> bytes:
    """앞에서 잘린 창의 첫 조각을 버린다. 줄바꿈이 있으면 첫 줄바꿈까지(잘린 줄 전체), 없으면 첫 공백·탭까지,
    그런 글자도 없으면 판단할 수 없어 전부 버린다.

    왜 줄 전체인가: `Authorization: ` 바로 뒤에서 잘린 `Basic <값>`처럼 앞 단서를 잃은 줄은 첫 낱말만 버려도 값이 남는다.
    줄바꿈이 없을 때만 낱말 단위로 버리는 이유: 긴 마지막 줄 하나가 원인일 때 안내문이 비지 않게 하려는 것이다.
    """
    if b"\n" in data:
        return data.partition(b"\n")[2]
    match = _FIRST_SEPARATOR.search(data)
    return data[match.end():] if match else b""


def _clean_line(line: str) -> str:
    """한 줄에서 ANSI escape를 지우고 탭은 공백으로, 나머지 제어 문자(유니코드 C 분류)는 지운다."""
    text = _ANSI_ESCAPE.sub("", line).replace("\t", " ")
    return "".join(character for character in text if not unicodedata.category(character).startswith("C")).strip()


def summarize_tool_output(log_text: str) -> str:
    """로그 끝부분에서 마지막 비어 있지 않은 줄 몇 개(stderr 우선, 없으면 stdout)를 안내문용 한 줄로 만든다.

    제어 문자를 지우고, 줄마다 URL의 사용자 정보를 redact_url로, 토큰·서명·Authorization 값을 mask_secrets로 가린 뒤,
    끝쪽 300자만 남긴다. 왜 가린 다음에 자르나: 먼저 자르면 `://`나 `token=`이 잘려 나가 비밀 조각이 가려지지 않은 채
    남을 수 있기 때문이다(로그 창의 잘린 첫 줄은 read_log_tail이 미리 버린다).
    """
    from .packages import redact_url  # packages가 이 모듈을 import하므로 순환을 피해 여기서 가져온다.

    stdout, _, stderr = log_text.rpartition(_STDERR_MARKER)
    for part in (stderr, stdout):
        lines = [mask_secrets(redact_url(cleaned)) for cleaned in map(_clean_line, part.splitlines()) if cleaned]
        if lines:
            text = " | ".join(lines[-_SUMMARY_LINES:])
            return text if len(text) <= _SUMMARY_LIMIT else "…" + text[-(_SUMMARY_LIMIT - 1):]
    return ""


def _failure_reason(error: CommandError, timeout: int) -> tuple[str, int | None]:
    """run_command 실패 문구를 (안내 문장, 시간 초과면 제한 초)로 바꾼다. 모르는 문구면 일반 실패로 본다."""
    message = str(error)
    exit_code = _EXIT_CODE_MESSAGE.fullmatch(message)
    if exit_code:
        return f"실행이 실패했습니다(종료 코드 {exit_code.group(1)}).", None
    if message == _TIMEOUT_MESSAGE:
        return f"실행이 시간 제한 {timeout}초를 넘겼습니다.", timeout
    if message == _NOT_STARTED_MESSAGE:
        return "실행 파일을 시작하지 못했습니다(설치돼 있지 않거나 실행 권한이 없습니다).", None
    if message == _OUTPUT_LIMIT_MESSAGE:
        return f"출력이 상한({_MAX_OUTPUT_BYTES // (1024 * 1024)} MiB)을 넘었습니다.", None
    return "실행이 실패했습니다.", None


def _tool_failure(argv: list[str], error: CommandError, timeout: int, log_path: Path | None,
                  output_log: Path) -> ToolFailure:
    """실패한 실행을 ToolFailure로 만든다. output_log는 실제 출력이 담긴 로그(호출자 로그 또는 임시 로그)다."""
    tail = read_log_tail(output_log)
    reason, timeout_seconds = _failure_reason(error, timeout)
    return ToolFailure(Path(argv[0]).name, log_path=log_path, output_tail=tail, reason=reason,
                       summary=summarize_tool_output(tail), timeout_seconds=timeout_seconds)


@contextlib.contextmanager
def _output_log(log_path: Path | None) -> Iterator[Path]:
    """run_command에 넘길 로그 경로를 준다. 호출자 로그가 있으면 그 옆의 새 0600 파일을 주고 끝나면 그 자리로 바꿔 넣고,
    없으면 시스템 임시 폴더의 0600 임시 로그를 주고 끝나면 지운다.

    왜 새 파일인가: run_command는 경로를 열어 자르며 쓰므로(링크를 따라감), 호출자 로그 자리에 하드·심볼릭 링크가 있으면
    연결된 다른 파일을 덮어쓴다. 왜 임시 로그인가: simctl·git처럼 로그 없이 부르는 명령도 실패 원인 줄을 안내문에 실으려는 것이다.
    """
    if log_path is not None:
        with replacing_file(Path(log_path)) as fresh_log:
            yield fresh_log
        return
    handle, name = tempfile.mkstemp(prefix="reproof-tool-", suffix=".log")
    os.close(handle)
    try:
        yield Path(name)
    finally:
        Path(name).unlink(missing_ok=True)


class HostToolRunner:
    """개발자 Mac에서 실제 명령을 실행하는 ToolRunner 구현."""

    def run(self, argv: list[str], *, cwd: Path, timeout: int, log_path: Path | None = None,
            env_extra: dict[str, str] | None = None) -> str:
        """기본 환경에 호출자의 env_extra를 덮어써 실행한다.

        반환: stdout. 실패·시간 초과 시 ToolFailure(실패 방식, 다듬은 마지막 출력, 로그 끝부분 포함). 왜: 호출자가
        CommandError의 일반 문구 대신 도구 이름·원인 줄·로그 위치를 담은 사용자용 오류를 받게 하려는 것이다.
        log_path가 없으면 임시 로그로 출력을 받고, 오류의 log_path는 None으로 둔다(임시 로그는 지워지므로).
        """
        environment = {**build_default_environment(), **(env_extra or {})}
        with _output_log(log_path) as output_log:
            try:
                return run_command(argv, str(cwd), timeout=timeout, max_output=_MAX_OUTPUT_BYTES,
                                   env_extra=environment, log_path=output_log)
            except CommandError as error:
                raise _tool_failure(argv, error, timeout, log_path, output_log) from error
