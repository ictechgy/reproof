"""간편 모드 테스트 공용 도구. Xcode나 실제 프로세스 없이 ToolRunner를 대신한다."""
from pathlib import Path


class FakeToolRunner:
    """미리 정한 응답을 돌려주는 ToolRunner 대역.

    왜: 단위 테스트가 xcodebuild 같은 실제 도구에 의존하지 않게 하려는 것이다.
    responses: (argv 앞부분, 출력 문자열 또는 던질 예외) 목록. 앞부분이 일치하는 첫 응답을 쓴다.
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
                return response
        raise AssertionError(f"일치하는 응답이 없습니다: {argv}")
