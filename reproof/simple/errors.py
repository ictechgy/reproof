"""간편 모드의 사용자용 오류. 항상 원인과 해결 방법을 함께 담는다."""
from pathlib import Path


class SimpleError(Exception):
    """사용자에게 보여줄 수 있는 간편 모드 오류.

    왜: 개발자가 스택 트레이스 대신 '무엇이 문제이고 어떻게 고치는지'를 바로 읽게 하려는 것이다.
    code: 기계가 구분하는 짧은 식별자. cause: 원인. remedy: 해결 방법. log_path: 상세 로그 위치(선택).
    """

    def __init__(self, code: str, cause: str, remedy: str, *, log_path: Path | None = None):
        """오류 필드를 저장한다. 예외 메시지는 cause로 둬 로그에서도 원인이 보이게 한다."""
        super().__init__(cause)
        self.code = code
        self.cause = cause
        self.remedy = remedy
        self.log_path = log_path

    def user_message(self) -> str:
        """'문제/해결(/로그)' 형식의 한국어 안내문을 만든다. 로그 경로가 없으면 로그 줄은 생략한다."""
        lines = [f"문제: {self.cause}", f"해결: {self.remedy}"]
        if self.log_path is not None:
            lines.append(f"로그: {self.log_path}")
        return "\n".join(lines)


CONFIG_TIMEOUT_REMEDY = ("시간이 더 필요하면 reproof.toml의 [build] timeout_seconds를 늘린 뒤 다시 실행하세요. "
                         "네트워크(패키지 받기)가 멈춰 있지 않은지도 확인하세요.")
"""빌드·패키지 해석처럼 설정의 build.timeout_seconds를 쓰는 단계가 시간 초과로 끝났을 때의 해결 방법."""

_TOOL_TIMEOUT_REMEDY = "도구가 멈춘 원인(네트워크, Simulator 상태 등)을 확인한 뒤 다시 실행하세요."
_LOG_REMEDY = "로그를 확인해 원인을 해결한 뒤 다시 실행하세요."
_OUTPUT_REMEDY = "위의 마지막 출력을 보고 원인을 해결한 뒤 다시 실행하세요."
_NO_OUTPUT_REMEDY = "도구가 아무것도 출력하지 않았습니다. `reproof app doctor`로 환경을 점검한 뒤 다시 실행하세요."


def _tool_failure_remedy(timeout_seconds: int | None, log_path: Path | None, summary: str) -> str:
    """시간 초과면 도구 정지 원인, 로그가 있으면 로그, 출력만 있으면 그 출력, 아무것도 없으면 환경 점검을 안내한다."""
    if timeout_seconds is not None:
        return _TOOL_TIMEOUT_REMEDY
    if log_path is not None:
        return _LOG_REMEDY
    return _OUTPUT_REMEDY if summary else _NO_OUTPUT_REMEDY


class ToolFailure(SimpleError):
    """외부 도구(xcodebuild, git 등)가 실패했을 때의 오류.

    command_name: 실행 파일 이름(전체 경로는 노출하지 않는다). log_path: 전체 출력 로그(임시 로그였으면 None).
    output_tail: 로그 끝부분(다듬지 않은 원문). 원인 파악용으로 호출자가 참고한다.
    reason: 실패 방식(종료 코드, 시간 초과 등)을 밝힌 문장. summary: 안내문에 넣을 마지막 출력(이미 다듬은 글).
    timeout_seconds: 시간 초과로 끝났을 때의 제한 시간(초), 아니면 None. 호출자가 설정 안내로 바꿀 때 쓴다.
    """

    def __init__(self, command_name: str, *, log_path: Path | None, output_tail: str,
                 reason: str = "실행이 실패했습니다.", summary: str = "", timeout_seconds: int | None = None):
        """도구 이름·실패 방식·마지막 출력으로 안내문을 만들고 출력 끝부분을 보관한다."""
        cause = f"{command_name} {reason}" + (f" 마지막 출력: {summary}" if summary else "")
        super().__init__("tool_failed", cause, _tool_failure_remedy(timeout_seconds, log_path, summary),
                         log_path=log_path)
        self.command_name = command_name
        self.output_tail = output_tail
        self.timeout_seconds = timeout_seconds
