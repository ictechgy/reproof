"""`reproof app` 명령 진입점. 하위 명령은 Task 12에서 구현하며 지금은 등록만 한다."""
import argparse
import sys
from collections.abc import Sequence

from .errors import SimpleError

_SUBCOMMANDS = {
    "init": "앱 프로젝트를 간편 모드용으로 설정합니다",
    "doctor": "빌드에 필요한 도구와 환경을 점검합니다",
    "build": "앱을 Simulator용으로 빌드합니다",
    "inspect": "빌드된 앱을 살펴봅니다",
}


def build_parser() -> argparse.ArgumentParser:
    """init, doctor, build, inspect 하위 명령을 등록한 파서를 만든다."""
    parser = argparse.ArgumentParser(prog="reproof app", description="iOS 앱 간편 모드")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in _SUBCOMMANDS.items():
        subparsers.add_parser(name, help=help_text)
    return parser


def run_command_by_name(command: str) -> int:
    """하위 명령을 실행한다. 아직 구현 전이라 항상 not_ready 오류를 낸다(Task 12에서 교체)."""
    raise SimpleError("not_ready", f"'{command}' 명령은 아직 준비되지 않았습니다.",
                      "다음 버전에서 제공됩니다. 업데이트 후 다시 시도하세요.")


def main(argv: Sequence[str]) -> int:
    """간편 모드 CLI를 실행한다.

    반환: 0 성공, 2 SimpleError, 130 KeyboardInterrupt. 오류 안내문은 stderr로 낸다.
    --help는 argparse가 SystemExit(0)로 처리한다.
    """
    arguments = build_parser().parse_args(list(argv))
    try:
        return run_command_by_name(arguments.command)
    except SimpleError as error:
        print(error.user_message(), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자가 중단했습니다.", file=sys.stderr)
        return 130
