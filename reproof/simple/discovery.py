"""Xcode 컨테이너·scheme·앱 타깃 자동 탐색. 회사 앱에서 질문을 최소화하려는 것이 목적이다."""
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import SimpleError
from .process import ToolRunner
from .workcopy import EXCLUDED_NAMES

# 탐색 깊이 상한(루트 아래 경로 구성요소 수). 모노레포에서 과도하게 내려가지 않기 위함이다.
_MAX_DEPTH = 3
# 컨테이너가 아니라 의존성 산출물이라 무시하는 디렉터리 이름.
_IGNORED_NAMES = EXCLUDED_NAMES | {"Pods", "node_modules"}
_APPLICATION_PRODUCT_TYPE = "com.apple.product-type.application"
_XCODEBUILD_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ContainerScan:
    """컨테이너 탐색 결과.

    workspaces/projects: 루트 기준 POSIX 상대 경로(정렬됨). generator: "xcodegen"|"tuist"|"".
    generator_spec: xcodegen이면 "project.yml", 아니면 None(Tuist는 단일 스펙 파일이 없다).
    """

    workspaces: tuple[str, ...]
    projects: tuple[str, ...]
    generator: str
    generator_spec: str | None


def detect_generator(root: Path) -> tuple[str, str | None]:
    """루트 바로 아래만 보고 (생성기, 스펙) 을 돌려준다.

    왜 루트만: 하위 폴더의 project.yml은 샘플·픽스처인 경우가 많아 오탐이 잦다.
    둘 다 있으면 어느 쪽이 진짜인지 알 수 없어 generator_ambiguous 를 낸다.
    """
    has_xcodegen = (root / "project.yml").is_file()
    has_tuist = any((root / name).exists() for name in ("Project.swift", "Workspace.swift", "Tuist"))
    if has_xcodegen and has_tuist:
        raise SimpleError("generator_ambiguous", "루트에 XcodeGen(project.yml)과 Tuist 설정이 함께 있습니다.",
                          "사용하지 않는 생성기 파일을 정리하거나 reproof 설정에서 생성기를 직접 지정하세요.")
    if has_xcodegen:
        return "xcodegen", "project.yml"
    return ("tuist", None) if has_tuist else ("", None)


def scan_containers(root: Path) -> ContainerScan:
    """루트에서 깊이 3까지 .xcworkspace/.xcodeproj 를 찾는다.

    `.xcodeproj` 내부(project.xcworkspace 등)와 무시 대상 디렉터리는 내려가지 않는다.
    반환 경로는 루트 기준 POSIX 상대 경로이며 정렬되어 결과가 항상 같다.
    """
    workspaces: list[str] = []
    projects: list[str] = []
    for current, directories, _ in os.walk(root):
        relative = Path(current).relative_to(root)
        directories[:] = sorted(_visit_candidates(directories, relative, workspaces, projects))
    generator, spec = detect_generator(root)
    return ContainerScan(tuple(sorted(workspaces)), tuple(sorted(projects)), generator, spec)


def _visit_candidates(directories: list[str], relative: Path, workspaces: list[str],
                      projects: list[str]) -> list[str]:
    """한 디렉터리의 하위 항목을 분류하고, 계속 내려가도 되는 이름만 돌려준다."""
    descend = []
    for name in directories:
        if name in _IGNORED_NAMES or len(relative.parts) + 1 > _MAX_DEPTH:
            continue
        path = (relative / name).as_posix()
        if name.endswith(".xcworkspace"):
            workspaces.append(path)
        elif name.endswith(".xcodeproj"):
            projects.append(path)
        else:
            descend.append(name)
    return descend


def choose_container(scan: ContainerScan) -> str | None:
    """자동으로 고를 수 있는 컨테이너를 돌려준다. 모호하면 None.

    생성기가 있으면 None 이다: 컨테이너는 생성 결과물이라 지금 존재하는 파일이 기준이 될 수 없고,
    init 이 생성 경로를 따로 처리한다. 생성기가 없을 때는 루트 바로 아래(구분자 없는 경로)의
    워크스페이스가 하나면 그것, 없고 프로젝트가 하나면 그것을 쓴다.
    """
    if scan.generator:
        return None
    for candidates in (scan.workspaces, scan.projects):
        top_level = [path for path in candidates if "/" not in path]
        if len(top_level) == 1:
            return top_level[0]
        if top_level:
            return None
    return None


def _container_flag(container: str) -> str:
    """컨테이너 확장자에 맞는 xcodebuild 플래그를 돌려준다."""
    return "-workspace" if container.endswith(".xcworkspace") else "-project"


def _parse_json_from_first_brace(output: str, opener: str, command_name: str):
    """경고 줄이 앞에 섞인 출력에서 첫 `opener`부터 JSON 을 읽는다. 실패하면 SimpleError."""
    start = output.find(opener)
    try:
        if start < 0:
            raise ValueError("JSON 시작 문자가 없습니다.")
        return json.JSONDecoder().raw_decode(output[start:])[0]
    except ValueError as error:
        raise SimpleError("xcodebuild_output_invalid", f"{command_name} 출력을 JSON 으로 읽지 못했습니다: {error}",
                          "Xcode 가 정상 설치됐는지 `xcodebuild -list` 를 직접 실행해 확인하세요.") from error


@dataclass(frozen=True)
class DiscoveryPaths:
    """탐색용 xcodebuild가 쓸 폴더. derived_data: -showBuildSettings의 DerivedData, packages_dir: 패키지 체크아웃 폴더.

    왜 필요한가: 지정하지 않으면 Xcode가 패키지를 해석하며 ~/Library/Developer/Xcode/DerivedData에 임시 사본 경로 기준
    폴더(패키지 포함, Wikipedia는 1.1 GB)를 만들고, 사본이 지워진 뒤에도 다시 쓰지 않는 고아로 남긴다.
    """

    derived_data: Path
    packages_dir: Path


def _location_arguments(paths: DiscoveryPaths | None, *, has_scheme: bool) -> list[str]:
    """탐색 폴더 인자를 만든다. paths가 없으면 빈 목록(기존 호출자 argv 유지).

    scheme이 있는 호출은 -derivedDataPath를, `-list`는 사용자 기본값 `-IDECustomDerivedDataLocation=<폴더>`를 쓴다.
    왜: `xcodebuild -list`는 -derivedDataPath를 거부하고("The flag -scheme, -testProductsPath, or -xctestrun is
    required when specifying -derivedDataPath."), 패키지 폴더만 주면 전역 DerivedData에 Logs 폴더가 남는다.
    사용자 기본값은 이 실행에만 적용되고 Xcode 설정 파일은 바꾸지 않는다(Xcode 27.0에서 확인).
    """
    if paths is None:
        return []
    if has_scheme:
        derived = ["-derivedDataPath", str(paths.derived_data)]
    else:
        derived = [f"-IDECustomDerivedDataLocation={paths.derived_data}"]
    return [*derived, "-clonedSourcePackagesDirPath", str(paths.packages_dir)]


def list_schemes(working_copy: Path, container: str, runner: ToolRunner, *,
                 paths: DiscoveryPaths | None = None) -> tuple[str, ...]:
    """`xcodebuild -list -json` 으로 scheme 이름 목록을 읽는다.

    워크스페이스는 {"workspace": ...}, 프로젝트는 {"project": ...} 형태라 둘 다 받는다.
    argv 는 리스트로 넘겨 공백·한글 경로가 한 인자로 유지된다. paths를 주면 패키지를 그 폴더에 받는다.
    """
    argv = ["xcodebuild", "-list", "-json", _container_flag(container), container,
            *_location_arguments(paths, has_scheme=False)]
    output = runner.run(argv, cwd=working_copy, timeout=_XCODEBUILD_TIMEOUT_SECONDS)
    data = _parse_json_from_first_brace(output, "{", "xcodebuild -list")
    section = data.get("workspace") or data.get("project") or {}
    return tuple(section.get("schemes", ()))


@dataclass(frozen=True)
class AppSettings:
    """앱 타깃 이름과 번들 ID."""

    target: str
    bundle_id: str


def read_app_settings(working_copy: Path, container: str, scheme: str, runner: ToolRunner, *,
                      paths: DiscoveryPaths | None = None) -> AppSettings:
    """scheme 의 빌드 설정에서 앱(application) 타깃의 이름과 번들 ID 를 읽는다.

    왜 PRODUCT_TYPE 으로 거르나: 위젯·확장(.appex)이 같은 scheme 에 섞여 있어도 .app 을 골라야 한다.
    후보가 여럿이면 TARGET_NAME 이 scheme 과 같은 것을 쓰고, 그래도 모호하면 app_target_ambiguous.
    paths를 주면 DerivedData와 패키지를 그 폴더에 둔다.
    """
    argv = ["xcodebuild", "-showBuildSettings", "-json", "-scheme", scheme, "-configuration", "Debug",
            "-sdk", "iphonesimulator", _container_flag(container), container,
            *_location_arguments(paths, has_scheme=True)]
    output = runner.run(argv, cwd=working_copy, timeout=_XCODEBUILD_TIMEOUT_SECONDS)
    entries = _parse_json_from_first_brace(output, "[", "xcodebuild -showBuildSettings")
    applications = _application_settings(entries)
    chosen = _pick_application(applications, scheme)
    return _to_app_settings(chosen, scheme)


def _application_settings(entries) -> list[dict]:
    """출력 배열에서 application 타깃의 buildSettings 만 모은다. 형식이 다르면 SimpleError."""
    if not isinstance(entries, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("buildSettings", {}), dict) for entry in entries):
        raise SimpleError("build_settings_invalid", "xcodebuild -showBuildSettings 출력 형식이 예상과 다릅니다.",
                          "Xcode 버전을 확인하고 `xcodebuild -showBuildSettings -json` 을 직접 실행해 보세요.")
    return [entry["buildSettings"] for entry in entries
            if entry.get("buildSettings", {}).get("PRODUCT_TYPE") == _APPLICATION_PRODUCT_TYPE]


def _to_app_settings(chosen: dict, scheme: str) -> AppSettings:
    """선택된 설정에서 타깃 이름과 번들 ID 를 꺼낸다. 하나라도 없으면 SimpleError."""
    target, bundle_id = chosen.get("TARGET_NAME"), chosen.get("PRODUCT_BUNDLE_IDENTIFIER")
    if not target or not bundle_id:
        raise SimpleError("build_settings_invalid",
                          f"scheme '{scheme}' 앱 타깃의 TARGET_NAME 또는 PRODUCT_BUNDLE_IDENTIFIER 가 비어 있습니다.",
                          "타깃의 Build Settings 에 Product Bundle Identifier 를 지정한 뒤 다시 실행하세요.")
    return AppSettings(target, bundle_id)


def _pick_application(applications: list[dict], scheme: str) -> dict:
    """앱 후보 중 하나를 고른다. 없거나 모호하면 사용자용 SimpleError."""
    if not applications:
        raise SimpleError("app_target_missing", f"scheme '{scheme}' 에서 앱(application) 타깃을 찾지 못했습니다.",
                          "앱 타깃을 빌드하는 scheme 을 지정하세요. 프레임워크·테스트 전용 scheme 은 쓸 수 없습니다.")
    if len(applications) > 1:
        applications = [item for item in applications if item.get("TARGET_NAME") == scheme] or applications
    if len(applications) > 1:
        names = ", ".join(item.get("TARGET_NAME", "?") for item in applications)
        raise SimpleError("app_target_ambiguous", f"scheme '{scheme}' 에 앱 타깃이 여러 개입니다: {names}",
                          "앱 타깃 하나만 빌드하는 scheme 을 지정하세요.")
    return applications[0]


def guess_test_scheme(schemes: Sequence[str], app_scheme: str) -> str | None:
    """단위 테스트 scheme 을 추측한다.

    이름에 Test 가 있고 UITest 가 없는 후보를 정렬해, 앱 scheme 이름으로 시작하는 첫 후보를 우선하고
    없으면 정렬상 첫 후보로 대체한다. 왜 정렬: 입력 순서와 무관하게 결과가 항상 같게 하려는 것이다.
    후보가 없으면 None.
    """
    candidates = sorted(name for name in schemes if "Test" in name and "UITest" not in name)
    preferred = [name for name in candidates if name.startswith(app_scheme)]
    return (preferred or candidates or [None])[0]
