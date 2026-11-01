#!/usr/bin/env python3
"""검증용 공개 앱을 pin.json에 적힌 커밋 그대로 받는다(간편 모드 spike용).

사용: python3 scripts/simple-fetch-app.py --pin examples/simple-ios/wikipedia/pin.json --output DIR

왜 따로 두나: 회사 앱 대신 쓰는 공개 앱(Wikipedia iOS 등)을 매번 같은 소스로 받아야 측정 결과를 비교할 수 있다.
그래서 브랜치가 아니라 40자 커밋 SHA로 고정하고, 받은 뒤 HEAD가 pin과 같은지 다시 확인한다.
표준 라이브러리만 쓰고(오류 형식만 저장소의 SimpleError를 따른다), 외부 명령은 argv 목록으로 실행한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproof.simple.errors import SimpleError  # noqa: E402  (저장소 루트를 경로에 넣은 뒤 가져온다)
from reproof.simple.packages import redact_url  # noqa: E402

COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
"""pin 커밋 형식: 소문자 16진수 40자. 짧은 SHA·브랜치 이름은 시간이 지나면 다른 커밋을 가리킬 수 있어 받지 않는다."""

ALLOWED_SCHEMES = ("https://", "file://")
"""받을 수 있는 저장소 주소 형식. 공개 앱은 https, 테스트의 로컬 bare 저장소는 file://로 받는다."""

GIT_TIMEOUT_SECONDS = 3600
"""git 명령 하나의 제한 시간(초). 큰 앱의 첫 checkout은 blob을 내려받느라 오래 걸릴 수 있다."""


@dataclass(frozen=True)
class Pin:
    """받을 앱의 고정 정보. repository: 저장소 주소, commit: 40자 커밋 SHA."""

    repository: str
    commit: str


def _invalid_pin(path: Path, detail: str) -> SimpleError:
    """pin 파일 형식 오류를 만든다. detail은 어느 값이 잘못됐는지 알려 준다."""
    return SimpleError("pin_invalid", f"pin 파일 {path}의 형식이 올바르지 않습니다: {detail}",
                       '{"repository": "https://...git", "commit": "<40자 커밋 SHA>"} 형식으로 고친 뒤 다시 실행하세요.')


def load_pin(path: Path) -> Pin:
    """pin.json을 읽고 검증해 Pin으로 돌려준다. 읽기·JSON·형식 오류는 pin_invalid.

    왜 주소 형식을 제한하나: 값이 `-`로 시작하는 등 git 옵션으로 해석되는 입력이나 ext:: 같은 명령 실행 transport를
    처음부터 막으려는 것이다. 사용자 정보(user@, user:비밀번호@)도 거절한다: pin.json은 커밋되는 파일이라 토큰이
    들어가면 안 되고, 거절 문구에도 주소를 싣지 않는다(판별은 로그 가리기와 같은 redact_url 규칙).
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _invalid_pin(path, str(error)) from error
    repository, commit = (document.get(key) if isinstance(document, dict) else None for key in ("repository", "commit"))
    if not isinstance(repository, str) or not repository.startswith(ALLOWED_SCHEMES):
        raise _invalid_pin(path, f"repository는 {', '.join(ALLOWED_SCHEMES)} 주소여야 합니다.")
    if redact_url(repository) != repository:
        raise _invalid_pin(path, "repository에 사용자 정보(user@ 또는 user:비밀번호@)를 넣을 수 없습니다. "
                                 "인증이 필요하면 git 자격 증명 도우미나 SSH 에이전트를 쓰세요.")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise _invalid_pin(path, "commit은 소문자 16진수 40자 SHA여야 합니다.")
    return Pin(repository, commit)


def refuse_existing(output: Path) -> None:
    """출력 위치에 무엇이든(깨진 링크 포함) 있으면 output_exists로 거절한다.

    왜: 이전에 받은 다른 소스와 섞이거나, 사용자의 폴더를 덮어쓰지 않게 하려는 것이다.
    """
    if os.path.lexists(output):
        raise SimpleError("output_exists", f"출력 위치가 이미 있습니다: {output}",
                          "다른 --output 경로를 지정하거나, 필요 없는 폴더임을 확인해 직접 지운 뒤 다시 실행하세요.")


def _failure_reason(error: Exception) -> str:
    """git 실패 이유를 짧게 만든다. 명령 argv(저장소 주소 포함)는 싣지 않는다.

    왜: CalledProcessError의 기본 문구는 argv 전체를 담아, 주소에 섞인 자격 증명이 안내문에 그대로 나올 수 있다.
    """
    if isinstance(error, subprocess.CalledProcessError):
        return f"종료 코드 {error.returncode}"
    if isinstance(error, subprocess.TimeoutExpired):
        return f"제한 시간 {GIT_TIMEOUT_SECONDS}초 초과"
    return getattr(error, "strerror", None) or type(error).__name__


def run_git(arguments: list[str], *, is_captured: bool = False) -> str:
    """git을 argv 목록으로 실행한다. is_captured면 stdout을 돌려주고, 아니면 진행 출력을 터미널에 그대로 보여 준다.

    GIT_TERMINAL_PROMPT=0으로 인증 입력을 기다리며 멈추지 않게 한다. 실패·시간 초과는 git_failed(argv 없이 단계·이유만).
    """
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subcommand = arguments[2] if arguments[0] == "-C" else arguments[0]
    try:
        result = subprocess.run(["git", *arguments], env=environment, timeout=GIT_TIMEOUT_SECONDS, check=True,
                                stdout=subprocess.PIPE if is_captured else None, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise SimpleError("git_failed", f"git {subcommand} 실행이 실패했습니다({_failure_reason(error)}).",
                          "위 git 출력에서 원인(네트워크, 저장소 주소, 커밋 존재 여부)을 확인한 뒤 다시 실행하세요.") from error
    return result.stdout or ""


def clone_pinned(pin: Pin, output: Path) -> None:
    """blob 없이 clone한 뒤 pin 커밋을 detached HEAD로 checkout한다(필요한 blob은 이때 받는다).

    왜 blob:none: 큰 앱의 전체 이력 파일 내용을 다 받지 않고, 고정 커밋의 파일만 받아 시간과 디스크를 줄인다.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--filter=blob:none", "--no-checkout", "--", pin.repository, str(output)])
    run_git(["-C", str(output), "checkout", "--detach", pin.commit])


def verify_head(output: Path, commit: str) -> None:
    """받은 저장소의 HEAD가 pin 커밋과 같은지 확인한다. 다르면 head_mismatch.

    왜 다시 확인하나: annotated tag 객체 SHA처럼 checkout은 되지만 다른 커밋으로 풀리는 값이 있어,
    "명령이 성공했다"만으로는 pin대로 받았다고 볼 수 없다.
    """
    head = run_git(["-C", str(output), "rev-parse", "--verify", "HEAD"], is_captured=True).strip()
    if head != commit:
        raise SimpleError("head_mismatch", f"받은 HEAD {head}가 pin 커밋 {commit}과(와) 다릅니다.",
                          "pin.json의 commit에 tag 객체가 아닌 커밋 SHA(`git rev-parse <tag>^{commit}`)를 적고 다시 실행하세요.")


def _discard(output: Path, original: BaseException) -> None:
    """이번 실행이 만든 출력 폴더를 지운다. 지우지 못하면 원래 오류에 남은 경로와 할 일을 덧붙인다(삼키지 않는다).

    SimpleError면 사용자가 보는 remedy에도 덧붙인다(workcopy의 정리 실패 처리와 같은 방식).
    """
    try:
        if os.path.lexists(output):
            shutil.rmtree(output)
    except OSError as cleanup_error:
        message = f"받다 만 폴더 {output}를 지우지 못했습니다({cleanup_error}). 직접 지운 뒤 다시 실행하세요."
        original.add_note(message)
        if isinstance(original, SimpleError):
            original.remedy = f"{original.remedy} 또한 {message}"


def fetch_app(pin: Pin, output: Path) -> None:
    """출력 위치를 확인하고 pin대로 받은 뒤 HEAD를 검증한다.

    실패·중단되면 이번 실행이 만든 폴더를 지운다. 왜: 다음 실행이 output_exists로 막히거나, pin과 다른·반쯤 받은
    소스가 정상 소스처럼 쓰이지 않게 하려는 것이다. 실행 전부터 있던 폴더는 refuse_existing이 먼저 막으므로 지우지 않는다.
    """
    refuse_existing(output)
    try:
        clone_pinned(pin, output)
        verify_head(output, pin.commit)
    except BaseException as error:
        _discard(output, error)
        raise


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    """--pin, --output 인자를 읽는다. 둘 다 필수다."""
    parser = argparse.ArgumentParser(description="검증용 공개 앱을 pin.json의 커밋 그대로 받습니다.")
    parser.add_argument("--pin", required=True, help="pin.json 경로 ({repository, commit})")
    parser.add_argument("--output", required=True, help="받을 폴더(없어야 함)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """스크립트 진입점. 반환: 0 성공, 2 SimpleError(안내문은 stderr), 130 사용자 중단."""
    arguments = parse_arguments(argv)
    output = Path(arguments.output).expanduser().absolute()
    try:
        pin = load_pin(Path(arguments.pin).expanduser())
        fetch_app(pin, output)
    except SimpleError as error:
        print(error.user_message(), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자가 중단했습니다.", file=sys.stderr)
        return 130
    print(f"받았습니다: {output}\n  HEAD: {pin.commit} (pin과 같음)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
