"""Simulator용 Debug 빌드 실행과 결과 .app 탐색."""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .errors import SimpleError
from .packages import PackageResolution
from .process import ToolRunner
from .workcopy import tree_digest

# DerivedData 안에서 Debug Simulator 산출물이 놓이는 상대 경로.
PRODUCTS_SUBPATH = Path("Build") / "Products" / "Debug-iphonesimulator"


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
    왜 macro·plugin 검증을 건너뛰는가: AI는 Package.swift·Package.resolved를 바꿀 수 없으므로
    실행되는 macro·plugin은 회사가 이미 쓰던 것으로 고정된다. 검증 프롬프트는 무인 실행을 막을 뿐이다.
    서명은 끈다(Simulator는 서명이 필요 없고, 개발자의 서명 자격 증명을 건드리지 않기 위해서다).
    """
    argv = ["xcodebuild", "build", f"-{config.container_kind}", config.container, "-scheme", config.scheme,
            "-configuration", "Debug", "-sdk", "iphonesimulator",
            "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", str(derived_data), "-clonedSourcePackagesDirPath", str(packages_dir),
            "-disableAutomaticPackageResolution"]
    if is_pinned:
        argv.append("-onlyUsePackageVersionsFromResolvedFile")
    argv += ["-skipMacroValidation", "-skipPackagePluginValidation",
             "CODE_SIGNING_ALLOWED=NO", "COMPILER_INDEX_STORE_ENABLE=NO"]
    return argv


def _bundle_identifier(app: Path) -> str | None:
    """.app의 Info.plist(XML·바이너리)에서 CFBundleIdentifier를 읽는다. 읽을 수 없으면 None.

    읽기 실패를 None으로 두는 이유: 다른 산출물(깨진 plist)이 있어도 일치하는 앱을 찾을 수 있어야 하고,
    일치하는 앱이 끝내 없으면 호출자가 app_bundle_missing으로 알린다.
    """
    try:
        with (app / "Info.plist").open("rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


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
    runner.run(argv, cwd=working_copy, timeout=config.build_timeout_seconds, log_path=log_path)
    app_path = find_app_bundle(derived_data / PRODUCTS_SUBPATH, config.bundle_id)
    return BuildProducts(app_path, config.bundle_id, derived_data, tree_digest(app_path))
