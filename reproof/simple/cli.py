"""`reproof app` 명령 진입점: init, doctor, build, inspect.

왜 질문을 줄이나: 회사 개발자가 설정 때문에 도구를 포기하지 않도록, 자동으로 정할 수 있는 값은 묻지 않고
모호한 것만 번호로 고르게 한다.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import re
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import CONFIG_FILE_NAME, ProjectConfig, SimulatorSettings, load_config, render_config, \
    resolve_launch_environment
from .discovery import (AppSettings, ContainerScan, DiscoveryPaths, choose_container, guess_test_scheme,
                        list_schemes, read_app_settings, scan_containers)
from .doctor import Check, run_doctor
from .errors import SimpleError
from .generation import generate_project
from .hierarchy import Element, element_at, parse_hierarchy
from .locator import capture_evidence, resolve
from .locking import project_lock
from .pipeline import PreparedBuild, _remove_tree, default_workspace_root, prepare_build
from .process import HostToolRunner, ToolRunner
from .runner_client import RunnerClient, RunnerSession, build_runner, free_loopback_port
from .simulator import SimulatorManager, launch_locale_settings
from .workcopy import materialize_working_copy

LAUNCH_SETTLE_SECONDS = 3.0
"""앱 실행 후 화면이 자리 잡을 때까지 기다리는 시간(초). 첫 화면 전환·로딩 중 계층을 읽지 않으려는 것이다."""

DEFAULT_DEVICE_TYPE = "iPhone 16"
"""init이 묻지 않고 쓰는 기본 Simulator 기기."""

_NAME_MAX_LENGTH = 40
_SUBCOMMANDS = {
    "init": "앱 프로젝트를 간편 모드용으로 설정합니다",
    "doctor": "빌드에 필요한 도구와 환경을 점검합니다",
    "build": "앱을 Simulator용으로 빌드합니다",
    "inspect": "앱을 실행해 화면 구조를 저장하고 요소를 확인합니다",
}


def build_parser() -> argparse.ArgumentParser:
    """init, doctor, build, inspect 하위 명령과 옵션을 등록한 파서를 만든다."""
    parser = argparse.ArgumentParser(prog="reproof app", description="iOS 앱 간편 모드")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {name: subparsers.add_parser(name, help=text) for name, text in _SUBCOMMANDS.items()}
    init = commands["init"]
    init.add_argument("--path", default=".", help="앱 저장소 폴더 (기본: 현재 폴더)")
    init.add_argument("--non-interactive", action="store_true", help="질문하지 않고, 모호하면 오류로 멈춥니다")
    init.add_argument("--force", action="store_true", help="이미 있는 reproof.toml을 덮어씁니다")
    for name in ("doctor", "build", "inspect"):
        commands[name].add_argument("--config", help="reproof.toml 경로 (기본: 현재 폴더부터 위로 찾음)")
    for name in ("build", "inspect"):
        commands[name].add_argument("--workspace", help="작업 공간 폴더 (기본: ~/.reproof 또는 REPROOF_HOME)")
    commands["inspect"].add_argument("--point", help="이 좌표(X,Y)의 요소를 확인합니다. 예: 195,420")
    commands["inspect"].add_argument("--output", help="화면 계층 JSON 저장 경로")
    return parser


def _new_runner() -> ToolRunner:
    """실제 명령 실행기를 만든다. 테스트에서 FakeToolRunner로 바꿔 끼우려고 함수로 분리했다."""
    return HostToolRunner()


# ---------- 공통: 설정 찾기 ----------

def find_config(start: Path) -> Path | None:
    """start부터 상위로 올라가며 처음 만나는 reproof.toml을 찾는다. 없으면 None.

    왜 멈추나: 저장소 밖(상위 폴더)의 낯선 설정을 자동으로 집어 쓰지 않도록, `.git`이 있는 폴더(포함)나
    홈 디렉터리(포함)에서 탐색을 끝낸다.
    """
    home = Path.home().absolute()
    for folder in (start, *start.parents):
        candidate = folder / CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
        if (folder / ".git").exists() or folder == home:
            return None
    return None


def _current_uid() -> int:
    """현재 사용자의 uid. 테스트에서 다른 소유자 상황을 흉내 내려고 함수로 분리했다."""
    return os.getuid()


def _require_trusted(path: Path) -> None:
    """설정 파일이 현재 사용자 소유가 아니면 config_untrusted로 거절한다. 파일이 없으면 load_config가 안내한다.

    왜: 설정에는 실행할 생성기·환경 변수가 담겨, 다른 사용자가 놓아 둔 파일을 그대로 따르면 위험하다.
    """
    try:
        owner = path.stat().st_uid
    except OSError:
        return  # 없거나 읽을 수 없는 파일은 이어서 load_config가 원인과 함께 오류를 낸다.
    if owner != _current_uid():
        raise SimpleError("config_untrusted", f"{path}의 소유자가 현재 사용자가 아닙니다.",
                          "내용을 확인한 뒤 `chown`으로 소유자를 바꾸거나, 본인 계정으로 `reproof app init --force`를 실행하세요.")


def _config_path(arguments: argparse.Namespace, *, is_required: bool) -> Path | None:
    """--config 값 또는 자동 탐색 결과를 돌려준다. 필수인데 없으면 init 안내와 함께 오류를 낸다."""
    found = Path(arguments.config).expanduser().absolute() if arguments.config else find_config(Path.cwd().absolute())
    if found is None and is_required:
        raise SimpleError("config_missing", "현재 폴더에서 저장소 루트(.git)나 홈 폴더까지 reproof.toml을 찾지 못했습니다.",
                          "앱 저장소 폴더에서 `reproof app init`을 먼저 실행하거나 --config로 경로를 지정하세요.")
    if found is not None:
        _require_trusted(found)
        if found.exists():
            print(f"설정 파일: {found}")
    return found


def _workspace_root(arguments: argparse.Namespace) -> Path:
    """--workspace 값이 있으면 그것, 없으면 기본 작업 공간(REPROOF_HOME 또는 ~/.reproof)을 쓴다."""
    if getattr(arguments, "workspace", None):
        return Path(arguments.workspace).expanduser().absolute()
    return default_workspace_root()


# ---------- doctor ----------

def format_check(check: Check) -> str:
    """점검 결과를 한 항목으로 만든다. claude는 선택 사항이라 ⓘ로 표시하고 실패 표시를 쓰지 않는다."""
    if check.name == "claude":
        suffix = "" if check.is_ok else f"\n  안내: {check.remedy}"
        return f"ⓘ {check.name}: {check.detail}{suffix}"
    if check.is_ok:
        return f"✓ {check.name}: {check.detail}"
    return f"✗ {check.name}: {check.detail}\n  해결: {check.remedy}"


def print_checks(checks: Sequence[Check]) -> bool:
    """점검 결과를 출력하고, claude를 제외한 모든 항목이 통과했는지 돌려준다."""
    for check in checks:
        print(format_check(check))
    return all(check.is_ok for check in checks if check.name != "claude")


def run_doctor_command(arguments: argparse.Namespace) -> int:
    """doctor 명령. 설정이 없어도 점검할 수 있게 하되, 있으면 그 설정의 런타임·생성기를 기준으로 본다."""
    path = _config_path(arguments, is_required=False)
    config = load_config(path) if path else None
    checks = run_doctor(config, runner=_new_runner(), workspace_root=_workspace_root(arguments))
    return 0 if print_checks(checks) else 2


# ---------- init ----------

class Prompter:
    """init의 질문을 맡는다. 질문 횟수를 세어 마지막에 알려 주고, 비대화 모드에서는 질문 대신 오류를 낸다."""

    def __init__(self, *, is_interactive: bool):
        """is_interactive가 False면 선택이 필요한 순간 init_needs_choice 오류를 낸다."""
        self.is_interactive = is_interactive
        self.question_count = 0

    def _ask(self, message: str) -> str:
        """input()으로 한 번 묻고 횟수를 센다. 입력이 닫혀 있으면(파이프 등) 안내 오류를 낸다."""
        self.question_count += 1
        try:
            return input(message).strip()
        except EOFError as error:
            raise SimpleError("input_closed", "질문에 답할 입력이 없습니다.",
                              "터미널에서 직접 실행하거나 --non-interactive를 쓰세요.") from error

    def choose(self, title: str, options: Sequence[str]) -> str:
        """번호로 하나를 고르게 한다. 비대화 모드면 후보를 모두 나열한 오류를 낸다."""
        if not self.is_interactive:
            raise SimpleError("init_needs_choice", f"{title} 후보가 여러 개라 자동으로 고를 수 없습니다: {', '.join(options)}",
                              "`--non-interactive` 없이 실행해 직접 고르거나, 앞 후보 중 하나로 reproof.toml을 직접 작성하세요.")
        print(f"{title}를 고르세요.")
        for number, option in enumerate(options, start=1):
            print(f"  {number}. {option}")
        answer = self._ask(f"번호 입력 (1-{len(options)}): ")
        if not answer.isdigit() or not 1 <= int(answer) <= len(options):
            raise SimpleError("choice_invalid", f"'{answer}'은(는) 올바른 번호가 아닙니다.",
                              f"1부터 {len(options)} 사이의 번호를 입력해 다시 실행하세요.")
        return options[int(answer) - 1]

    def confirm_test_scheme(self, schemes: Sequence[str], guess: str | None) -> str | None:
        """테스트 scheme 추측값을 보여 주고 Enter로 수락하게 한다. 비대화 모드에서는 묻지 않고 추측값을 쓴다."""
        if guess is None or not self.is_interactive:
            return guess
        answer = self._ask(f"테스트 scheme은 '{guess}'(으)로 추정했습니다. Enter=수락, 다른 이름 입력, -=사용 안 함: ")
        if answer == "":
            return guess
        if answer == "-":
            return None
        if answer not in schemes:
            raise SimpleError("test_scheme_invalid", f"'{answer}' scheme이 없습니다. 후보: {', '.join(schemes)}",
                              "목록의 이름을 그대로 입력하거나 Enter로 추정값을 수락하세요.")
        return answer


def slugify_name(folder_name: str) -> str:
    """폴더 이름을 설정 이름 규칙(소문자·숫자·대시, 최대 40자)에 맞는 slug로 바꾼다. 비면 'app'."""
    slug = re.sub(r"[^a-z0-9]+", "-", folder_name.lower()).strip("-")[:_NAME_MAX_LENGTH].strip("-")
    return slug or "app"


def _pick_container(scan: ContainerScan, prompter: Prompter) -> str:
    """자동으로 정해지면 그것을, 아니면 후보에서 고르게 한다. 후보가 없으면 오류를 낸다."""
    chosen = choose_container(replace(scan, generator="", generator_spec=None))
    if chosen is not None:
        return chosen
    options = [*scan.workspaces, *scan.projects]
    if not options:
        raise SimpleError("container_missing", ".xcworkspace 또는 .xcodeproj를 찾지 못했습니다(깊이 3까지 탐색).",
                          "앱 저장소 폴더에서 실행하고 있는지 확인하세요. XcodeGen·Tuist 프로젝트면 생성 스펙이 루트에 있어야 합니다.")
    return options[0] if len(options) == 1 else prompter.choose("Xcode 프로젝트", options)


def _application_schemes(search_root: Path, container: str, schemes: Sequence[str], runner: ToolRunner,
                         paths: DiscoveryPaths) -> dict[str, AppSettings]:
    """scheme 중 앱 타깃이 있는 것만 {scheme: 앱 설정}으로 모은다. paths는 xcodebuild가 쓸 탐색 폴더다.

    왜 읽기 실패도 건너뛰나: 테스트·패키지 scheme은 Simulator 대상이 없어 xcodebuild가 오류를 내기도 하므로, scheme
    하나의 조회 실패로 init 전체를 멈추지 않는다. 다만 앱 scheme이 하나도 없으면 첫 실패 원인을 그대로 보여 준다.
    """
    found: dict[str, AppSettings] = {}
    failures: list[SimpleError] = []
    for scheme in schemes:
        try:
            found[scheme] = read_app_settings(search_root, container, scheme, runner, paths=paths)
        except SimpleError as error:
            print(f"건너뜀: {scheme} ({error.cause})")
            failures.append(error)
    if not found:
        reportable = [error for error in failures if error.code != "app_target_missing"]
        if reportable:
            raise reportable[0]
    return found


def _pick_application(applications: dict[str, AppSettings], prompter: Prompter) -> tuple[str, AppSettings]:
    """앱 scheme이 하나면 그것, 여러 개면 고르게 한다. 없으면 오류를 낸다."""
    if not applications:
        raise SimpleError("app_target_missing", "앱(application) 타깃을 빌드하는 scheme을 찾지 못했습니다.",
                          "Xcode에서 앱 타깃의 scheme을 Shared로 체크하고 저장소에 커밋한 뒤 다시 실행하세요.")
    names = list(applications)
    scheme = names[0] if len(names) == 1 else prompter.choose("앱 scheme", names)
    return scheme, applications[scheme]


def _config_from_project(root: Path, search_root: Path, scan: ContainerScan, generator: tuple[str, str | None],
                         runner: ToolRunner, prompter: Prompter, paths: DiscoveryPaths) -> ProjectConfig:
    """search_root(원본 또는 생성된 사본)에서 컨테이너·scheme·앱 설정을 정해 ProjectConfig를 만든다.

    paths: xcodebuild가 쓸 탐색 폴더(전역 DerivedData에 고아 폴더를 남기지 않으려는 것).
    """
    container = _pick_container(scan, prompter)
    schemes = list_schemes(search_root, container, runner, paths=paths)
    scheme, app = _pick_application(_application_schemes(search_root, container, schemes, runner, paths), prompter)
    test_scheme = prompter.confirm_test_scheme(schemes, guess_test_scheme(schemes, scheme))
    return ProjectConfig(
        name=slugify_name(root.name), container=container, generator=generator[0] or "none",
        generator_spec=generator[1], scheme=scheme, app_target=app.target, bundle_id=app.bundle_id,
        test_scheme=test_scheme, simulator=SimulatorSettings(DEFAULT_DEVICE_TYPE, "latest"))


@contextlib.contextmanager
def _discovery_copy(root: Path, scan: ContainerScan, workspace_root: Path,
                    runner: ToolRunner) -> Iterator[tuple[Path, DiscoveryPaths]]:
    """원본을 임시 사본으로 복사하고(생성기가 있으면 생성까지 해서) (사본 경로, 탐색 폴더)를 준다. 끝나면 사본을 지운다.

    왜 항상 사본에서: xcodebuild -list 등이 Package.resolved·.build를 쓸 수 있고 생성기는 .xcodeproj를 쓰므로,
    init이 원본 저장소에 남기는 파일을 reproof.toml 하나로 제한하려는 것이다. 생성 로그는 사본 밖(작업 공간 logs)에
    둬서 실패했을 때도 안내된 경로에서 읽을 수 있다.
    탐색 폴더: DerivedData는 사본과 함께 지워지는 임시 폴더 안, 패키지는 작업 공간의 `cache/spm/discovery`에 둔다
    (init을 다시 해도 패키지를 다시 받지 않게 하고, ~/Library/Developer/Xcode/DerivedData를 쓰지 않으려는 것).
    """
    tmp_root = workspace_root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "logs").mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="init-", dir=tmp_root))
    try:
        copy = materialize_working_copy(root, directory / "source")
        provisional = ProjectConfig(
            name=slugify_name(root.name), container="", generator=scan.generator or "none",
            generator_spec=scan.generator_spec, scheme="", app_target="", bundle_id="", test_scheme=None,
            simulator=SimulatorSettings(DEFAULT_DEVICE_TYPE, "latest"))
        generate_project(copy.root, provisional, runner, log_path=workspace_root / "logs" / "init-generate.log")
        yield copy.root, DiscoveryPaths(directory / "dd", workspace_root / "cache" / "spm" / "discovery")
    finally:
        _remove_tree(directory)


def discover_config(root: Path, runner: ToolRunner, workspace_root: Path, prompter: Prompter) -> ProjectConfig:
    """프로젝트를 탐색해 ProjectConfig를 만든다. 탐색은 항상 임시 사본에서 하고 원본은 읽기만 한다."""
    scan = scan_containers(root)
    generator = (scan.generator, scan.generator_spec)
    with _discovery_copy(root, scan, workspace_root, runner) as (copy_root, paths):
        return _config_from_project(root, copy_root, scan_containers(copy_root), generator, runner, prompter, paths)


def _refuse_symlink(target: Path) -> None:
    """reproof.toml이 심볼릭 링크면 config_symlink로 거절한다(깨진 링크 포함).

    왜: 링크를 따라 쓰면 링크가 가리키는 다른 파일(예: 홈의 설정)을 덮어쓰게 된다.
    """
    if target.is_symlink():
        raise SimpleError("config_symlink", f"{target}은(는) 심볼릭 링크라 쓸 수 없습니다.",
                          "링크를 지우거나 실제 파일로 바꾼 뒤 다시 실행하세요.")


def _write_config(target: Path, config: ProjectConfig) -> None:
    """설정을 같은 폴더의 임시 파일에 쓰고 검증한 뒤 os.replace로 바꿔 넣는다.

    왜 임시 파일: 링크를 따라가 쓰지 않고, 검증에 실패하거나 중간에 끊겨도 기존 파일이 깨지지 않게 하려는 것이다.
    """
    _refuse_symlink(target)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".reproof-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(render_config(config))
        load_config(temp_path)
        os.chmod(temp_path, 0o644)  # mkstemp의 0600 대신 일반 소스 파일 권한으로 둬 팀이 함께 읽게 한다.
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def run_init_command(arguments: argparse.Namespace) -> int:
    """init 명령. reproof.toml을 만들고 doctor 결과와 다음 단계를 보여 준다."""
    root = Path(arguments.path).expanduser().absolute()
    target = root / CONFIG_FILE_NAME
    if not root.is_dir():
        raise SimpleError("path_invalid", f"앱 폴더를 찾을 수 없습니다: {root}", "--path에 앱 저장소 폴더를 지정하세요.")
    _refuse_symlink(target)
    if target.exists() and not arguments.force:
        raise SimpleError("config_exists", f"{target}이(가) 이미 있습니다.",
                          "그대로 쓰려면 `reproof app build`를, 새로 만들려면 --force를 붙여 다시 실행하세요.")
    runner, workspace_root = _new_runner(), default_workspace_root()
    prompter = Prompter(is_interactive=not arguments.non_interactive)
    config = discover_config(root, runner, workspace_root, prompter)
    _write_config(target, config)
    print_init_summary(target, config, prompter.question_count)
    print_checks(run_doctor(config, runner=runner, workspace_root=workspace_root))
    print("다음 단계: reproof app build")
    return 0


def print_init_summary(target: Path, config: ProjectConfig, question_count: int) -> None:
    """저장한 설정의 핵심 값과 질문 횟수를 출력한다."""
    print(f"저장했습니다: {target}")
    print(f"  프로젝트: {config.container}  scheme: {config.scheme}  테스트 scheme: {config.test_scheme or '없음'}")
    print(f"  앱 번들 ID: {config.bundle_id}  Simulator: {config.simulator.device_type} ({config.simulator.runtime})")
    print(f"질문 {question_count}개")
    print("환경 점검:")


# ---------- build / inspect ----------

def _load_project(arguments: argparse.Namespace) -> tuple[ProjectConfig, Path]:
    """설정을 읽고 (설정, 앱 저장소 루트)를 돌려준다. 루트는 reproof.toml이 있는 폴더다."""
    path = _config_path(arguments, is_required=True)
    return load_config(path), path.parent


def _build(arguments: argparse.Namespace, runner: ToolRunner) -> tuple[ProjectConfig, PreparedBuild, Path]:
    """설정을 읽고 빌드(완성된 빌드가 있으면 재사용)한다. (설정, 빌드, 작업 공간)을 돌려준다."""
    config, root = _load_project(arguments)
    workspace_root = _workspace_root(arguments)
    prepared = prepare_build(root, config, workspace_root=workspace_root, runner=runner)
    return config, prepared, workspace_root


def run_build_command(arguments: argparse.Namespace) -> int:
    """build 명령. 앱을 Simulator용으로 빌드하고 빌드 ID를 보여 준다."""
    _, prepared, _ = _build(arguments, _new_runner())
    print(f"빌드 ID: {prepared.build_id}")
    return 0


def parse_point(text: str | None) -> tuple[float, float] | None:
    """`--point X,Y`를 숫자 두 개로 읽는다. 없으면 None, 형식이 틀리면 point_invalid."""
    if text is None:
        return None
    try:
        x_text, y_text = text.split(",")
        x, y = float(x_text), float(y_text)
        if math.isfinite(x) and math.isfinite(y):
            return x, y
    except ValueError:
        pass  # 아래의 point_invalid 오류로 한 번에 안내한다.
    raise SimpleError("point_invalid", f"--point 값 '{text}'을(를) 좌표로 읽지 못했습니다. X,Y 형식의 숫자 두 개여야 합니다.",
                      "예: --point 195,420")


def prepare_simulator(config: ProjectConfig, prepared: PreparedBuild, runner: ToolRunner) -> str:
    """전용 Simulator를 확보해 초기화·표시 고정·앱 설치를 하고 udid를 돌려준다. 끄지는 않는다."""
    simulators = SimulatorManager(runner)
    udid = simulators.ensure_device(config.name, config.simulator)
    simulators.reset(udid)
    simulators.apply_presentation(udid)
    simulators.install_app(udid, prepared.products.app_path)
    return udid


def launch_app(client: RunnerClient, config: ProjectConfig, launch_environment: dict[str, str]) -> None:
    """로케일 설정과 설정의 launch 인자, 미리 풀어 둔 launch 환경(launch_environment)으로 앱을 실행하고 화면이 자리 잡을
    때까지 기다린다."""
    locale_arguments, locale_environment = launch_locale_settings(config.simulator)
    client.launch([*locale_arguments, *config.launch_arguments], {**locale_environment, **launch_environment})
    time.sleep(LAUNCH_SETTLE_SECONDS)


def save_hierarchy(client: RunnerClient, output: Path) -> Element:
    """런타임이 준 계층 문서를 UTF-8 JSON으로 저장하고 파싱한 루트 요소를 돌려준다."""
    document = client.command("hierarchy")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"화면 계층 저장: {output}")
    return parse_hierarchy(document.get("root"))


def describe_point(root: Element, point: tuple[float, float]) -> None:
    """좌표의 요소와 로케이터 근거, 다시 찾았을 때의 강도를 출력한다."""
    element = element_at(root, *point)
    if element is None:
        raise SimpleError("point_no_element", f"좌표 {point[0]:g},{point[1]:g}에 요소가 없습니다.",
                          "저장된 계층 JSON의 frame을 보고 화면 안의 좌표를 지정하세요.")
    evidence = capture_evidence(root, element)
    resolution = resolve(root, evidence)
    print(f"요소: 종류={element.element_type} 라벨={element.label or '(없음)'} identifier={element.identifier or '(없음)'}")
    print("근거: " + json.dumps(evidence.to_document(), ensure_ascii=False))
    print(f"재탐색: strength={resolution.strength} matched_by={resolution.matched_by}")


def inspect_output_path(arguments: argparse.Namespace, prepared: PreparedBuild) -> Path:
    """--output 또는 빌드 폴더의 inspect-<시각>.json 경로를 돌려준다."""
    if arguments.output:
        return Path(arguments.output).expanduser().absolute()
    return prepared.directory / f"inspect-{datetime.now():%Y%m%d-%H%M%S}.json"


def run_inspect_command(arguments: argparse.Namespace) -> int:
    """inspect 명령. 빌드 → 전용 Simulator 준비 → 실행기 세션 → 앱 실행 → 계층 저장(→ 좌표 요소 확인).

    reproof-<이름> Simulator는 끄지 않고 둔다(다음 실행을 빠르게 하고, 이 명령이 시작하지 않은 것은 건드리지 않으려는 것).
    빌드부터 세션 끝까지 프로젝트 잠금을 쥔다. 왜: 다른 터미널의 같은 프로젝트 실행이 세션 도중 같은 Simulator를
    초기화하거나 빌드 폴더를 지우지 못하게 하려는 것이다.
    """
    point = parse_point(arguments.point)  # 빌드 전에 검사해 잘못된 입력으로 몇 분을 쓰지 않게 한다.
    config, root = _load_project(arguments)
    launch_environment = resolve_launch_environment(config, os.environ)  # 빠진 env: 참조도 빌드·부팅 전에 알린다.
    workspace_root = _workspace_root(arguments)
    with project_lock(workspace_root, config.name):
        hierarchy_root = _inspect_locked(arguments, config, root, workspace_root, launch_environment)
    if point is not None:
        describe_point(hierarchy_root, point)
    return 0


def _inspect_locked(arguments: argparse.Namespace, config: ProjectConfig, root: Path, workspace_root: Path,
                    launch_environment: dict[str, str]) -> Element:
    """프로젝트 잠금을 쥔 상태에서 빌드 → Simulator 준비 → 실행기 → 세션 → 앱 실행 → 계층 저장을 하고 계층 루트를 돌려준다."""
    runner = _new_runner()
    prepared = prepare_build(root, config, workspace_root=workspace_root, runner=runner)
    udid = prepare_simulator(config, prepared, runner)
    products = build_runner(workspace_root / "runner", runner)
    session = RunnerSession(products, udid=udid, target_bundle=config.bundle_id, port=free_loopback_port(),
                            log_path=prepared.directory / "logs" / "inspect-session.log")
    with session as client:
        launch_app(client, config, launch_environment)
        return save_hierarchy(client, inspect_output_path(arguments, prepared))


_COMMANDS = {"init": run_init_command, "doctor": run_doctor_command, "build": run_build_command,
             "inspect": run_inspect_command}

# 디스크 공간 부족과 권한 문제를 뜻하는 errno. 해결 안내를 고르는 데 쓴다.
_DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def _os_error_remedy(number: int | None) -> str:
    """errno에 맞는 해결 방법을 고른다. 모르는 errno면 권한과 여유 공간을 함께 확인하라고 안내한다."""
    if number in _DISK_FULL_ERRNOS:
        return "디스크 여유 공간을 확보한 뒤 다시 실행하세요. 작업 공간(~/.reproof 또는 REPROOF_HOME)의 오래된 빌드를 지워도 됩니다."
    if number in _PERMISSION_ERRNOS:
        return "해당 파일·폴더의 권한(`ls -l`)을 확인해 읽기·쓰기가 되게 하거나, 읽기 전용 위치라면 다른 위치를 지정한 뒤 다시 실행하세요."
    return "해당 파일·폴더의 권한과 디스크 여유 공간을 확인한 뒤 다시 실행하세요."


def os_error_as_simple_error(error: OSError) -> SimpleError:
    """파일·디스크 작업의 OSError를 원인(strerror)·파일 이름·해결 방법을 담은 file_operation_failed로 바꾼다.

    왜: 읽을 수 없는 파일, 가득 찬 디스크, 읽기 전용 폴더 같은 환경 문제는 코드 결함이 아니므로 트레이스백 대신
    무엇을 고치면 되는지 알려 주려는 것이다.
    """
    names = [str(name) for name in (error.filename, error.filename2) if name is not None]
    where = f" ({', '.join(names)})" if names else ""
    reason = error.strerror or str(error) or type(error).__name__
    return SimpleError("file_operation_failed", f"파일 작업이 실패했습니다: {reason}{where}", _os_error_remedy(error.errno))


def write_crash_log(workspace_root: Path, details: str) -> Path:
    """트레이스백(details)을 `<작업 공간>/logs/crash-<시각>.log`에 소유자만 읽게(0600) 쓰고 경로를 돌려준다.

    왜 파일인가: 사용자 화면에는 할 일만 보이고, 개발자가 원인을 찾을 전체 기록은 첨부할 수 있게 남기려는 것이다.
    O_EXCL로 만들어 기존 파일·링크를 덮어쓰지 않는다.
    """
    folder = workspace_root / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"crash-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(details)
    return path


def report_internal_error(arguments: argparse.Namespace) -> str:
    """처리 중인 예상 밖 예외를 crash 로그에 남기고 사용자 안내문을 돌려준다.

    로그를 쓰지 못하면 그 이유와 원래 트레이스백을 안내문에 그대로 싣는다(어떤 경우에도 원인을 버리지 않으려는 것).
    """
    details = traceback.format_exc()
    try:
        path = write_crash_log(_workspace_root(arguments), details)
    except Exception as error:  # 로그 쓰기 실패가 원래 오류를 가리지 않게, 이유를 밝히고 원래 내용을 화면에 낸다.
        return f"내부 오류입니다. 로그 파일을 쓰지 못해({error}) 내용을 아래에 출력합니다. 이 내용을 첨부해 알려 주세요.\n{details}"
    return f"내부 오류입니다. 이 로그를 첨부해 알려 주세요: {path}"


def main(argv: Sequence[str]) -> int:
    """간편 모드 CLI를 실행한다.

    반환: 0 성공, 2 오류(SimpleError·OSError·예상 밖 예외) 또는 doctor 점검 실패, 130 KeyboardInterrupt.
    오류 안내문은 stderr로 낸다. OSError는 원인·파일·해결로, 그 밖의 예외는 crash 로그 경로로 안내한다.
    --help는 argparse가 SystemExit(0)로 처리한다.
    """
    arguments = build_parser().parse_args(list(argv))
    try:
        return _COMMANDS[arguments.command](arguments)
    except SimpleError as error:
        print(error.user_message(), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자가 중단했습니다.", file=sys.stderr)
        return 130
    except OSError as error:
        print(os_error_as_simple_error(error).user_message(), file=sys.stderr)
        return 2
    except Exception:
        print(report_internal_error(arguments), file=sys.stderr)
        return 2
