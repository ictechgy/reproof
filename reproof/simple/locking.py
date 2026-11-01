"""같은 작업 공간 자원을 두 실행이 동시에 쓰지 못하게 하는 파일 잠금(fcntl.flock, 기다리지 않음).

지키는 것: 프로젝트 잠금(`projects/<이름>/.lock`)은 그 프로젝트의 staging·partial 빌드 폴더를, 실행기 잠금
(`runner/.lock`)은 공유하는 runner-src·runner-dd를, 패키지 캐시 잠금(`cache/spm/<키>.lock`)은 여러 프로젝트가 함께 쓰는
SwiftPM 체크아웃 폴더를 지킨다. Simulator 잠금(`<임시 폴더>/reproof-<uid>/sim-<기기>.lock`)은
작업 공간과 무관하게 사용자 전체에서 `reproof-*` 기기 하나의 초기화·설치·세션을 지킨다.
왜 flock인가: 프로세스가 끝나면(강제 종료 포함) 커널이 잠금을 풀어, 남은 잠금 파일 때문에 영영 막히는 일이 없다.
왜 기다리지 않나: 다른 실행이 몇 분 걸리는 빌드 중일 수 있어, 멈춘 듯 보이게 하지 않고 바로 이유를 알린다.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import tempfile
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


def _private_lock_directory() -> Path:
    """사용자별 전역 잠금 폴더 `<임시 폴더>/reproof-<uid>`를 0700으로 만들거나 확인해 돌려준다.

    왜 확인하나: 임시 폴더가 여러 사용자가 함께 쓰는 /tmp일 수 있어, 다른 사용자가 미리 만든 폴더나 링크에 잠금 파일을
    두면 잠금이 엇갈리거나 남의 폴더에 파일을 만들게 된다. 링크이거나 폴더가 아니거나 내 것이 아니면 lock_directory_untrusted.
    """
    directory = Path(tempfile.gettempdir()) / f"reproof-{os.getuid()}"
    with contextlib.suppress(FileExistsError):  # 이미 있으면 아래에서 종류·소유자를 확인한다.
        directory.mkdir(mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SimpleError("lock_directory_untrusted", f"잠금 폴더 {directory}가 링크이거나 다른 사용자의 것입니다.",
                          "그 경로를 확인해 직접 지우거나 TMPDIR을 본인만 쓰는 폴더로 바꾼 뒤 다시 실행하세요.")
    os.chmod(directory, 0o700)
    return directory


def simulator_lock(device_name: str) -> contextlib.AbstractContextManager[None]:
    """`reproof-*` 기기 하나의 사용자 전역 잠금. 기기 준비(생성·초기화·설치)부터 세션 끝까지 쥔다. 잠겨 있으면 simulator_busy.

    왜 작업 공간 밖인가: 작업 공간(--workspace·REPROOF_HOME)이 다른 두 실행은 프로젝트 잠금이 서로 달라, 같은 기기를
    동시에 초기화해 진행 중인 세션을 깨뜨릴 수 있기 때문이다.
    """
    path = _private_lock_directory() / f"sim-{device_name}.lock"
    return exclusive_lock(path, lambda: SimpleError(
        "simulator_busy", f"다른 터미널에서 Simulator {device_name}를 쓰는 중입니다(잠금 파일: {path}).",
        "그 터미널의 `reproof app inspect`가 끝난 뒤 다시 실행하세요. 잠금은 그 실행이 끝나면(강제 종료 포함) 자동으로 "
        "풀리므로 잠금 파일을 지울 필요는 없습니다."))


def package_cache_lock(cache_dir: Path) -> contextlib.AbstractContextManager[None]:
    """패키지 캐시 폴더 옆 `<cache_dir>.lock` 잠금. 그 캐시를 쓰는 패키지 해석과 빌드(또는 init 탐색) 동안 쥔다.

    왜: 캐시 키가 Package.resolved 내용(또는 탐색용 고정 이름)이라 다른 프로젝트·작업 트리도 같은 폴더를 쓰는데, 두
    xcodebuild가 같은 -clonedSourcePackagesDirPath에 동시에 체크아웃하면 캐시가 깨진다. 잠겨 있으면 package_cache_busy.
    """
    path = cache_dir.with_name(cache_dir.name + ".lock")
    return exclusive_lock(path, lambda: SimpleError(
        "package_cache_busy", f"다른 터미널에서 같은 Swift 패키지 캐시({cache_dir})를 쓰는 중입니다(잠금 파일: {path}).",
        "그 터미널의 `reproof app build`·`inspect`·`init`이 패키지 해석·빌드를 마친 뒤 다시 실행하세요. 잠금은 그 실행이 "
        "끝나면 자동으로 풀립니다."))
