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


class ToolFailure(SimpleError):
    """외부 도구(xcodebuild, git 등)가 실패했을 때의 오류.

    command_name: 실행 파일 이름(전체 경로는 노출하지 않는다). log_path: 전체 출력 로그.
    output_tail: 로그 끝부분. 원인 파악용으로 호출자가 참고한다.
    """

    def __init__(self, command_name: str, *, log_path: Path | None, output_tail: str):
        """도구 이름으로 안내문을 만들고 출력 끝부분을 보관한다."""
        super().__init__(
            "tool_failed",
            f"{command_name} 실행이 실패했습니다.",
            "로그를 확인해 원인을 해결한 뒤 다시 실행하세요.",
            log_path=log_path,
        )
        self.command_name = command_name
        self.output_tail = output_tail
