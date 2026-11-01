"""XcodeGen/Tuist 프로젝트 생성 실행. 생성된 프로젝트가 없는 저장소에서 빌드 전에 한 번 돈다."""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import ProjectConfig
from .errors import SimpleError
from .process import ToolRunner

# 생성기 실행 제한 시간(초). 대형 Tuist 프로젝트도 충분하도록 넉넉히 둔다.
GENERATE_TIMEOUT_SECONDS = 600

# 생성기별 설치 안내. 팀마다 설치 방식이 다를 수 있어 Tuist는 대안도 함께 적는다.
_INSTALL_HINTS = {
    "xcodegen": "`brew install xcodegen`으로 설치한 뒤 다시 실행하세요.",
    "tuist": "`mise install tuist`(또는 팀이 쓰는 설치 방법)으로 설치한 뒤 다시 실행하세요.",
}

# xcodegen이 spec 경로를 따로 받지 못했을 때 쓰는 기본 파일 이름.
_DEFAULT_XCODEGEN_SPEC = "project.yml"


def generator_tool_path(generator: str) -> Path | None:
    """생성기 실행 파일 경로를 PATH에서 찾는다.

    generator: 'xcodegen' 또는 'tuist'. 반환: 경로, 없으면 None(그 외 값도 None).
    """
    if generator not in _INSTALL_HINTS:
        return None
    found = shutil.which(generator)
    return Path(found) if found else None


def _require_generator(generator: str) -> None:
    """생성기가 설치돼 있지 않으면 설치 방법을 담은 generator_missing 오류를 낸다."""
    if generator_tool_path(generator) is None:
        raise SimpleError("generator_missing", f"{generator}를 이 Mac에서 찾을 수 없습니다.",
                          _INSTALL_HINTS[generator])


def _xcodegen_invocation(working_copy: Path, config: ProjectConfig) -> tuple[list[str], Path]:
    """xcodegen argv와 cwd를 만든다. cwd는 spec이 있는 폴더라 spec 안의 상대 경로가 맞게 풀린다."""
    spec = (working_copy / (config.generator_spec or _DEFAULT_XCODEGEN_SPEC))
    return ["xcodegen", "generate", "--spec", spec.name], spec.parent


def generate_project(working_copy: Path, config: ProjectConfig, runner: ToolRunner, *, log_path: Path) -> None:
    """설정된 생성기(xcodegen/tuist)로 Xcode 프로젝트를 만든다. generator가 none이면 아무것도 하지 않는다.

    working_copy: 소스 작업 복사본 루트. runner: 명령 실행기. log_path: 생성기 출력 로그.
    생성기가 없으면 SimpleError(generator_missing), 실행 실패는 ToolFailure가 그대로 전달된다.
    """
    if config.generator == "none":
        return
    _require_generator(config.generator)
    if config.generator == "xcodegen":
        argv, cwd = _xcodegen_invocation(working_copy, config)
    else:
        argv, cwd = ["tuist", "generate", "--no-open"], working_copy
    runner.run(argv, cwd=cwd, timeout=GENERATE_TIMEOUT_SECONDS, log_path=log_path)
