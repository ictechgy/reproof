"""같은 작업 공간 자원을 두 실행이 동시에 쓰지 못하게 하는 파일 잠금(fcntl.flock, 기다리지 않음).

지키는 것: 프로젝트 잠금(`projects/<이름>/.lock`)은 그 프로젝트의 staging·partial 빌드 폴더와 `reproof-<이름>`
Simulator를, 실행기 잠금(`runner/.lock`)은 공유하는 runner-src·runner-dd를 지킨다.
왜 flock인가: 프로세스가 끝나면(강제 종료 포함) 커널이 잠금을 풀어, 남은 잠금 파일 때문에 영영 막히는 일이 없다.
왜 기다리지 않나: 다른 실행이 몇 분 걸리는 빌드 중일 수 있어, 멈춘 듯 보이게 하지 않고 바로 이유를 알린다.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .errors import SimpleError

LOCK_FILE_NAME = ".lock"
"""잠금 파일 이름. 지키는 폴더 바로 아래에 둔다(staging-*·*.partial 정리 패턴과 겹치지 않는다)."""

_held: dict[Path, int] = {}
"""이 프로세스가 이미 쥔 잠금(경로 → 겹친 횟수).

왜: inspect가 프로젝트 잠금을 쥔 채 prepare_build를 부르는데, 같은 파일을 다른 디스크립터로 다시 flock하면 자기 자신과
충돌한다. 그래서 같은 프로세스 안에서는 횟수만 센다. 주의: 스레드 사이의 배제는 하지 않는다(CLI는 한 스레드로 돈다).
"""


def _acquire(path: Path, busy: Callable[[], SimpleError]) -> int:
    """path를 0600으로 열고(링크는 따라가지 않음) 기다리지 않는 배타 flock을 건다. 이미 잠겨 있으면 busy()를 던진다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, BlockingIOError):
            raise busy() from error
        raise
    return descriptor


def _release(key: Path, descriptor: int | None) -> None:
    """겹친 횟수를 줄이고, 처음 잠근 쪽이면 디스크립터를 닫아 잠금을 푼다."""
    _held[key] -= 1
    if _held[key] == 0:
        del _held[key]
    if descriptor is not None:
        os.close(descriptor)


@contextlib.contextmanager
def exclusive_lock(path: Path, busy: Callable[[], SimpleError]) -> Iterator[None]:
    """with 동안 path의 배타 잠금을 쥔다. 다른 프로세스(또는 다른 디스크립터)가 쥐고 있으면 busy()가 만든 오류를 던진다.

    같은 프로세스가 이미 쥔 잠금이면 다시 걸지 않고 겹쳐 쥔다(_held 참고).
    """
    key = path.absolute()
    descriptor = None if key in _held else _acquire(key, busy)
    _held[key] = _held.get(key, 0) + 1
    try:
        yield
    finally:
        _release(key, descriptor)


def project_lock(workspace_root: Path, project_name: str) -> contextlib.AbstractContextManager[None]:
    """`<작업 공간>/projects/<이름>/.lock` 잠금. 빌드 준비와 inspect 세션 전체 동안 쥔다. 잠겨 있으면 project_busy."""
    path = workspace_root / "projects" / project_name / LOCK_FILE_NAME
    return exclusive_lock(path, lambda: SimpleError(
        "project_busy", f"다른 터미널에서 같은 프로젝트({project_name})를 실행 중입니다(잠금 파일: {path}).",
        "그 터미널의 `reproof app build`·`inspect`가 끝난 뒤 다시 실행하세요. 잠금은 그 실행이 끝나면(강제 종료 포함) "
        "자동으로 풀리므로 잠금 파일을 지울 필요는 없습니다."))


def runner_lock(output_root: Path) -> contextlib.AbstractContextManager[None]:
    """`<실행기 폴더>/.lock` 잠금. 실행기 소스 복사·빌드 동안 쥔다. 잠겨 있으면 runner_busy."""
    path = output_root / LOCK_FILE_NAME
    return exclusive_lock(path, lambda: SimpleError(
        "runner_busy", f"다른 터미널에서 실행기(SimpleRunner)를 준비하는 중입니다(잠금 파일: {path}).",
        "그 터미널의 `reproof app inspect`가 실행기 준비를 마친 뒤 다시 실행하세요. 잠금은 그 실행이 끝나면 자동으로 "
        "풀립니다."))
