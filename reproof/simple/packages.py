"""Swift 패키지 해석. 개발자 Mac의 기존 git 인증을 그대로 쓰고, 인증 실패를 알기 쉬운 안내로 바꾼다."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ProjectConfig, config_digest
from .errors import CONFIG_TIMEOUT_REMEDY, SimpleError, ToolFailure
from .process import ToolRunner

# 인증 실패를 뜻하는 출력 문구(소문자 비교).
_AUTH_MARKERS = (
    "authentication failed",
    "could not read username",
    "permission denied (publickey)",
    "terminal prompts disabled",
    "repository not found",
)

# 인증 판별에 읽는 로그 끝부분 크기(바이트).
_LOG_SCAN_BYTES = 64 * 1024

# 실패 문구에서 저장소 주소를 찾는 패턴: 스킴 URL 먼저, 없으면 scp 형식(git@host:org/repo).
_SCHEME_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
_USERINFO_PATTERN = re.compile(r"(://)[^\s'\"]*@")
_SCP_URL = re.compile(r"[\w.-]+@[\w.-]+:[\w./~-]+")


@dataclass(frozen=True)
class PackageResolution:
    """패키지 해석 결과. cache_dir: 클론 캐시, resolved_digest: Package.resolved 해시(없으면 None), is_pinned: 버전 고정 여부."""

    cache_dir: Path
    resolved_digest: str | None
    is_pinned: bool


def find_package_resolved(working_copy: Path, config: ProjectConfig) -> Path | None:
    """container 안의 Package.resolved 경로를 돌려준다. 파일이 없으면 None.

    왜 위치가 둘인가: workspace는 자기 안에, project는 내부 project.xcworkspace 안에 Xcode가 저장하기 때문이다.
    """
    container = working_copy / config.container
    if config.container_kind == "project":
        container = container / "project.xcworkspace"
    resolved = container / "xcshareddata" / "swiftpm" / "Package.resolved"
    return resolved if resolved.is_file() else None


def _cache_dir_for(cache_root: Path, config: ProjectConfig, resolved: Path | None) -> tuple[Path, str | None]:
    """캐시 폴더와 resolved 해시를 정한다. 고정 파일이 없으면 설정 지문으로 격리한다."""
    if resolved is None:
        return cache_root / "spm" / ("unpinned-" + config_digest(config)[:16]), None
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return cache_root / "spm" / digest, digest


def _resolve_argv(config: ProjectConfig, cache_dir: Path, is_pinned: bool) -> list[str]:
    """xcodebuild -resolvePackageDependencies argv를 만든다. 시스템 git(scmProvider system)을 써 기존 인증을 따른다."""
    argv = ["xcodebuild", "-resolvePackageDependencies", f"-{config.container_kind}", config.container,
            "-scheme", config.scheme, "-clonedSourcePackagesDirPath", str(cache_dir), "-scmProvider", "system"]
    if is_pinned:
        argv.append("-onlyUsePackageVersionsFromResolvedFile")
    return argv


def resolve_packages(working_copy: Path, config: ProjectConfig, *, cache_root: Path, runner: ToolRunner,
                     log_path: Path, warn: Callable[[str], None]) -> PackageResolution:
    """패키지를 공유 캐시에 해석한다.

    Package.resolved가 있으면 그 버전만 쓰고, 없으면 경고 후 고정 없이 해석한다(실행마다 버전이 달라질 수 있음).
    인증 실패는 package_auth_failed, 그 외 도구 실패는 package_resolution_failed로 바꿔 낸다.
    """
    resolved = find_package_resolved(working_copy, config)
    cache_dir, digest = _cache_dir_for(cache_root, config, resolved)
    if resolved is None:
        warn("Package.resolved가 없어 패키지 버전을 고정하지 못합니다. 실행할 때마다 의존성 버전이 달라질 수 있으니 "
             "Package.resolved를 저장소에 커밋하는 것을 권장합니다.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    argv = _resolve_argv(config, cache_dir, resolved is not None)
    try:
        runner.run(argv, cwd=working_copy, timeout=config.build_timeout_seconds, log_path=log_path)
    except ToolFailure as failure:
        raise classify_package_failure(failure) from failure
    return PackageResolution(cache_dir=cache_dir, resolved_digest=digest, is_pinned=resolved is not None)


def redact_url(url: str) -> str:
    """'://' 뒤부터 같은 토큰의 마지막 '@'까지를 지운다. scp 형식(git@host:path)과 URL이 아닌 글은 그대로 둔다.

    왜 urlsplit을 쓰지 않는가: 비밀번호·토큰에 `/`, `?`, `#`가 있으면 urlsplit이 netloc을 일찍 끊어
    자격 증명이 그대로 남기 때문이다. 공백·따옴표 전까지 가장 긴 구간(마지막 '@')을 지워 안전 쪽으로 기운다.
    """
    return _USERINFO_PATTERN.sub(r"\1", url)


def _failure_text(failure: ToolFailure) -> str:
    """출력 끝부분과 로그 파일 마지막 64KiB를 합친 판별용 텍스트를 만든다. 로그를 못 읽으면 출력 끝부분만 쓴다."""
    text = failure.output_tail
    if failure.log_path is not None:
        try:
            data = Path(failure.log_path).read_bytes()
        except OSError:
            return text  # 로그는 보조 정보다. 읽기 실패로 원래 실패의 분류를 막지 않는다.
        text += "\n" + data[-_LOG_SCAN_BYTES:].decode("utf-8", "replace")
    return text


def _first_repository_url(text: str) -> str | None:
    """텍스트에서 처음 보이는 저장소 주소(redact 전)를 찾는다. 없으면 None."""
    match = _SCHEME_URL.search(text) or _SCP_URL.search(text)
    return match.group(0).rstrip(".,;:)") if match else None


def classify_package_failure(failure: ToolFailure) -> SimpleError:
    """패키지 해석 실패를 인증 문제인지 일반 실패인지 가려 사용자용 오류로 바꾼다.

    반환: SimpleError(package_auth_failed 또는 package_resolution_failed). 저장소 URL은 redact_url로 가린다.
    """
    text = _failure_text(failure)
    if not any(marker in text.lower() for marker in _AUTH_MARKERS):
        if failure.timeout_seconds is not None:  # 이 단계의 제한 시간은 설정의 build.timeout_seconds다.
            return SimpleError("package_resolution_failed", f"Swift 패키지를 해석하지 못했습니다: {failure.cause}",
                               CONFIG_TIMEOUT_REMEDY, log_path=failure.log_path)
        return SimpleError("package_resolution_failed", "Swift 패키지를 해석하지 못했습니다.",
                           "로그에서 실패한 패키지를 확인하고 의존성 설정을 고친 뒤 다시 실행하세요.",
                           log_path=failure.log_path)
    found = _first_repository_url(text)
    url = redact_url(found) if found else None
    target = url or "패키지 저장소"
    return SimpleError("package_auth_failed", f"{target}에 접근할 인증이 없습니다.",
                       f"이 Mac 터미널에서 `git ls-remote {url or '<저장소 URL>'}`이 되는지 확인한 뒤 다시 실행하세요.",
                       log_path=failure.log_path)
