"""환경 점검(doctor). 빌드 전에 빠진 도구를 한 번에 알려 주려는 것이 목적이다."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .errors import SimpleError
from .generation import generator_tool_path
from .process import ToolRunner
from .simulator import list_runtimes, pick_runtime

# 작업 공간에 필요한 최소 남은 공간(바이트). 작업 사본과 DerivedData가 앱마다 수 GB를 차지한다.
MIN_FREE_BYTES = 30 * 10**9
# xcodebuild -version 제한 시간(초).
_XCODE_VERSION_TIMEOUT = 60
# 생성기별 설치 안내(generation 모듈과 같은 문구 방향).
_GENERATOR_REMEDY = {
    "xcodegen": "`brew install xcodegen`으로 설치하세요.",
    "tuist": "`mise install tuist`(또는 팀이 쓰는 설치 방법)으로 설치하세요.",
}


@dataclass(frozen=True)
class Check:
    """점검 항목 한 개. name: 항목 이름, is_ok: 통과 여부, detail: 현재 상태 설명, remedy: 실패 시 해결 방법."""

    name: str
    is_ok: bool
    detail: str
    remedy: str


def _check_xcode(runner: ToolRunner) -> Check:
    """`xcodebuild -version`이 실행되는지 본다. 실패하면 설치·선택 방법을 안내한다."""
    remedy = "Xcode를 설치하고 `sudo xcode-select -s /Applications/Xcode.app`으로 선택하세요."
    try:
        output = runner.run(["xcodebuild", "-version"], cwd=Path.cwd(), timeout=_XCODE_VERSION_TIMEOUT)
    except (SimpleError, OSError):
        return Check("xcode", False, "xcodebuild를 실행하지 못했습니다.", remedy)
    lines = output.strip().splitlines()
    return Check("xcode", True, lines[0] if lines else "xcodebuild 사용 가능", "")


def _check_ios_runtime(config: ProjectConfig | None, runner: ToolRunner) -> Check:
    """설정이 요구하는(없으면 latest) iOS Simulator 런타임이 설치돼 있는지 본다."""
    requested = config.simulator.runtime if config else "latest"
    try:
        runtime = pick_runtime(list_runtimes(runner), requested)
    except SimpleError as error:
        return Check("ios_runtime", False, error.cause, error.remedy)
    return Check("ios_runtime", True, runtime.name, "")


def _check_git() -> Check:
    """git이 PATH에 있는지 본다. 소스 버전 식별과 SPM 해석에 쓴다."""
    if shutil.which("git"):
        return Check("git", True, "git 사용 가능", "")
    return Check("git", False, "git을 찾을 수 없습니다.", "`xcode-select --install`로 명령줄 도구를 설치하세요.")


def _check_generator(generator: str) -> Check:
    """설정의 프로젝트 생성기(xcodegen/tuist)가 설치돼 있는지 본다."""
    if generator_tool_path(generator) is not None:
        return Check("generator", True, f"{generator} 사용 가능", "")
    remedy = _GENERATOR_REMEDY.get(generator, f"{generator}를 설치하세요.")
    return Check("generator", False, f"{generator}를 찾을 수 없습니다.", remedy)


def _nearest_existing_parent(path: Path) -> Path:
    """아직 만들어지지 않은 작업 공간이어도 남은 공간을 재도록 존재하는 가장 가까운 상위 폴더를 찾는다."""
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _check_disk(workspace_root: Path) -> Check:
    """작업 공간이 놓일 디스크의 남은 공간이 30 GB 이상인지 본다."""
    free_bytes = shutil.disk_usage(_nearest_existing_parent(workspace_root)).free
    detail = f"남은 공간 {free_bytes / 10**9:.1f} GB"
    if free_bytes >= MIN_FREE_BYTES:
        return Check("disk", True, detail, "")
    return Check("disk", False, f"{detail} (30 GB 필요)", "불필요한 파일을 지우거나 REPROOF_HOME을 여유 있는 디스크로 지정하세요.")


def _check_claude() -> Check:
    """claude CLI가 PATH에 있는지만 본다(호출하지 않는다). AI 수정에만 필요해 실패해도 안내용이다."""
    if shutil.which("claude"):
        return Check("claude", True, "claude 사용 가능 (AI 수정에 필요)", "")
    return Check("claude", False, "claude를 찾을 수 없습니다. AI 수정에 필요합니다.",
                 "AI 수정을 쓰려면 Claude Code를 설치하세요. 빌드·살펴보기에는 필요하지 않습니다.")


def run_doctor(config: ProjectConfig | None, *, runner: ToolRunner, workspace_root: Path) -> tuple[Check, ...]:
    """환경 점검을 정해진 순서(xcode, ios_runtime, git, generator, disk, claude)로 실행한다.

    config: 없으면(아직 init 전) 런타임은 latest 기준, 생성기 점검은 건너뛴다.
    반환: Check 튜플. generator 항목은 설정에 생성기가 있을 때만 들어간다.
    """
    checks = [_check_xcode(runner), _check_ios_runtime(config, runner), _check_git()]
    if config is not None and config.generator != "none":
        checks.append(_check_generator(config.generator))
    checks += [_check_disk(workspace_root), _check_claude()]
    return tuple(checks)
