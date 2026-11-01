"""Simulator용 Debug 빌드 실행과 결과 .app 탐색."""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .errors import CONFIG_TIMEOUT_REMEDY, SimpleError, ToolFailure
from .packages import PackageResolution
from .process import ToolRunner
from .workcopy import tree_digest

# DerivedData 안에서 Debug Simulator 산출물이 놓이는 상대 경로.
PRODUCTS_SUBPATH = Path("Build") / "Products" / "Debug-iphonesimulator"

BUILD_RECIPE_VERSION = 3
"""Simulator 빌드 방법(simulator_build_argv 규칙)의 버전. 산출물이 달라지는 방법 변경 때마다 1씩 올린다.

빌드 ID에 들어가므로, 올리면 이전 완성 빌드를 재사용하지 않고 새로 빌드한다.
기록: 1 = 서명 끔(CODE_SIGNING_ALLOWED=NO), 2 = ad-hoc 서명(App Group 등 entitlement 유지),
3 = 매크로·플러그인 검증을 기본으로 유지(건너뛰기는 [build] skip_package_validation = true일 때만).
"""

# xcodebuild -version 제한 시간(초).
_VERSION_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class BuildProducts:
    """빌드 산출물. app_path: .app 경로, bundle_id: 앱 식별자, derived_data: DerivedData 경로,
    products_digest: .app 트리의 지문(같은 앱인지 나중에 확인하는 용도)."""

    app_path: Path
    bundle_id: str
    derived_data: Path
    products_digest: str


def simulator_build_argv(config: ProjectConfig, *, derived_data: Path, packages_dir: Path,
                         is_pinned: bool = True) -> list[str]:
    """Simulator용 Debug 빌드 xcodebuild argv를 만든다.

    config: 프로젝트 설정. derived_data: 산출물 폴더. packages_dir: 해석된 패키지 캐시.
    is_pinned: Package.resolved가 있을 때만 그 버전만 쓰도록 강제한다(없으면 해석 결과를 그대로 쓴다).
    macro·plugin 검증은 기본으로 유지하고, config.skip_package_validation이 true일 때만 건너뛴다. 왜: Package.resolved가
    있다는 것은 개발자가 그 매크로·플러그인(빌드 중 임의 코드 실행)을 신뢰했다는 뜻이 아니다. 처음 받는 의존성이나
    자동으로 갱신된 resolved도 있으므로, 신뢰는 Xcode의 확인(Trust & Enable)이나 설정의 명시적 동의로만 정한다.
    서명은 인증서가 필요 없는 ad-hoc(`CODE_SIGN_IDENTITY=-`, Xcode의 "Sign to Run Locally")으로 한다.
    왜 끄지 않나: Simulator에서도 App Group 같은 entitlement는 서명 단계에서 실행 파일에 들어가므로,
    CODE_SIGNING_ALLOWED=NO로 끄면 App Group 컨테이너를 쓰는 앱이 시작하자마자 죽는다(Wikipedia에서 확인).
    ad-hoc 서명은 키체인의 개발자 인증서와 프로비저닝 프로필을 쓰지 않으므로 서명 자격 증명은 건드리지 않는다.
    """
    argv = ["xcodebuild", "build", f"-{config.container_kind}", config.container, "-scheme", config.scheme,
            "-configuration", "Debug", "-sdk", "iphonesimulator",
            "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", str(derived_data), "-clonedSourcePackagesDirPath", str(packages_dir),
            "-disableAutomaticPackageResolution"]
    if is_pinned:
        argv.append("-onlyUsePackageVersionsFromResolvedFile")
    if config.skip_package_validation:
        argv += ["-skipMacroValidation", "-skipPackagePluginValidation"]
    argv += ["CODE_SIGN_IDENTITY=-", "COMPILER_INDEX_STORE_ENABLE=NO"]
    return argv


def read_xcode_version(runner: ToolRunner) -> str:
    """`xcodebuild -version` 출력(앞뒤 공백 제거)을 돌려준다. 실패하면 xcode_version_unavailable.

    왜: Xcode가 바뀌면 같은 소스라도 산출물이 달라지므로 빌드 ID에 넣는다. 현재 폴더에서 실행한다(프로젝트를 읽지 않는다).
    """
    try:
        return runner.run(["xcodebuild", "-version"], cwd=Path.cwd(), timeout=_VERSION_TIMEOUT_SECONDS).strip()
    except ToolFailure as failure:
        raise SimpleError("xcode_version_unavailable", "Xcode 버전을 확인하지 못했습니다(`xcodebuild -version` 실패).",
                          "`xcode-select -p`로 선택된 Xcode를 확인하고 `reproof app doctor`로 점검한 뒤 다시 실행하세요.",
                          log_path=failure.log_path) from failure


# 매크로·플러그인 신뢰 검증 실패를 뜻하는 출력 문구(소문자 비교).
_VALIDATION_MARKERS = ("must be enabled before it can be used",)
_LOG_SCAN_BYTES = 64 * 1024


def is_validation_failure(failure: ToolFailure) -> bool:
    """빌드 실패 출력이 macro·plugin 신뢰 검증 때문인지 판별한다(출력 끝부분과 로그 끝 64KiB 검사).

    왜 정확한 문구만 보나: macro·plugin·trust·enable 낱말 조합 같은 넓은 규칙은 일반 빌드 오류를 검증 문제로 잘못 안내했다.
    """
    text = failure.output_tail
    if failure.log_path is not None:
        try:
            text += "\n" + Path(failure.log_path).read_bytes()[-_LOG_SCAN_BYTES:].decode("utf-8", "replace")
        except OSError:
            pass  # 로그는 보조 정보다. 읽지 못해도 출력 끝부분으로 판별한다.
    lowered = text.lower()
    return any(marker in lowered for marker in _VALIDATION_MARKERS)


def validation_required_error(failure: ToolFailure) -> SimpleError:
    """검증 실패를 package_validation_required 오류로 바꾼다. 해결 방법은 Xcode에서 신뢰하거나 설정으로 명시적으로 건너뛰기."""
    return SimpleError("package_validation_required",
                       "이 프로젝트의 Swift 매크로·패키지 플러그인은 신뢰 확인이 필요합니다(빌드 중 코드를 실행하므로 "
                       "툴이 대신 허용하지 않습니다).",
                       "Xcode에서 프로젝트를 한 번 열어 해당 매크로·플러그인을 신뢰(Trust & Enable)한 뒤 다시 실행하세요. "
                       "회사가 검토한 패키지만 쓴다면 reproof.toml의 [build]에 skip_package_validation = true를 넣어 "
                       "검증을 건너뛸 수도 있습니다.", log_path=failure.log_path)


def _bundle_identifier(app: Path) -> str | None:
    """.app의 Info.plist(XML·바이너리)에서 CFBundleIdentifier를 읽는다. 읽을 수 없으면 None.

    읽기 실패를 None으로 두는 이유: 다른 산출물(깨진 plist)이 있어도 일치하는 앱을 찾을 수 있어야 하고,
    일치하는 앱이 끝내 없으면 호출자가 app_bundle_missing으로 알린다.
    """
    try:
        with (app / "Info.plist").open("rb") as handle:
            document = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(document, dict):
        return None  # 루트가 사전이 아닌 plist는 앱 Info.plist가 아니므로 일치하지 않는 것으로 본다.
    return document.get("CFBundleIdentifier")


def find_app_bundle(products_dir: Path, bundle_id: str) -> Path:
    """products_dir 바로 아래 .app 중 bundle_id가 일치하는 하나를 찾는다.

    없으면 app_bundle_missing, 둘 이상이면 app_bundle_ambiguous. 왜 이름이 아니라 bundle id인가:
    확장·위젯·테스트 호스트 등 .app이 여러 개일 수 있고 이름은 scheme과 다를 수 있기 때문이다.
    """
    matches = sorted(app for app in products_dir.glob("*.app") if _bundle_identifier(app) == bundle_id)
    if not matches:
        raise SimpleError("app_bundle_missing", f"빌드 결과에서 bundle id가 {bundle_id}인 .app을 찾지 못했습니다.",
                          "reproof.toml의 project.bundle_id가 앱 타깃의 Bundle Identifier와 같은지 확인하세요.")
    if len(matches) > 1:
        names = ", ".join(app.name for app in matches)
        raise SimpleError("app_bundle_ambiguous", f"bundle id가 {bundle_id}인 .app이 여러 개입니다: {names}",
                          "scheme이 같은 앱을 중복으로 만들지 않는지 확인하고 project.scheme을 고치세요.")
    return matches[0]


def build_for_simulator(working_copy: Path, config: ProjectConfig, *, derived_data: Path,
                        packages: PackageResolution, runner: ToolRunner, log_path: Path) -> BuildProducts:
    """Simulator용 Debug 빌드를 실행하고 결과 .app을 찾아 BuildProducts로 돌려준다.

    working_copy: 빌드 cwd(상대 container 경로의 기준). 실패하면 ToolFailure, 앱이 없으면 SimpleError.
    """
    argv = simulator_build_argv(config, derived_data=derived_data, packages_dir=packages.cache_dir,
                                is_pinned=packages.is_pinned)
    try:
        runner.run(argv, cwd=working_copy, timeout=config.build_timeout_seconds, log_path=log_path)
    except ToolFailure as failure:
        if not config.skip_package_validation and is_validation_failure(failure):
            raise validation_required_error(failure) from failure
        if failure.timeout_seconds is not None:
            failure.remedy = CONFIG_TIMEOUT_REMEDY  # 이 단계의 제한 시간은 설정의 build.timeout_seconds다.
        raise
    app_path = find_app_bundle(derived_data / PRODUCTS_SUBPATH, config.bundle_id)
    return BuildProducts(app_path, config.bundle_id, derived_data, tree_digest(app_path))
