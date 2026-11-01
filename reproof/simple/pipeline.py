"""빌드 파이프라인: 식별 → 작업 사본 → (생성) → 패키지 → Debug 빌드. 끊긴 빌드는 이어 쓰지 않고 다시 시작한다."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from .build import BUILD_RECIPE_VERSION, BuildProducts, _bundle_identifier, build_for_simulator, read_xcode_version
from .config import ProjectConfig, config_digest
from .errors import SimpleError
from .generation import generate_project
from .identity import BuildIdentity, capture_build_identity
from .locking import package_cache_lock, project_lock
from .packages import package_cache_dir, resolve_packages
from .process import ToolRunner
from .workcopy import WorkingCopy, _make_tree_writable, add_include_hint, materialize_working_copy, tree_digest

# 완성된 빌드의 산출물 요약 파일 이름. 이 파일이 있고 앱이 실제로 있어야 "완성"이다.
PRODUCTS_FILE = "products.json"


@dataclass(frozen=True)
class PreparedBuild:
    """준비된 빌드. build_id/identity는 식별 정보, working_copy/products는 완성 디렉터리 기준 경로, directory는 빌드 폴더."""

    build_id: str
    identity: BuildIdentity
    working_copy: WorkingCopy
    products: BuildProducts
    directory: Path


def default_workspace_root(environ: Mapping[str, str] = os.environ) -> Path:
    """작업 공간 루트를 돌려준다. REPROOF_HOME이 있으면 그 값, 없으면 ~/.reproof."""
    configured = environ.get("REPROOF_HOME")
    root = Path(configured) if configured else Path.home() / ".reproof"
    return root.expanduser().absolute()


TOOL_DIRECTORIES = ("projects", "cache", "cache/spm", "runner", "logs", "tmp")
"""작업 공간 바로 아래에 도구가 만드는 폴더. 모두 0700으로 둔다(prepare_workspace 참고)."""


def _current_uid() -> int:
    """현재 사용자의 uid. 테스트에서 다른 소유자 상황을 흉내 내려고 함수로 분리했다."""
    return os.getuid()


def _untrusted(path: Path, detail: str) -> SimpleError:
    """작업 공간 폴더를 믿을 수 없을 때의 workspace_untrusted 오류를 만든다(경로와 이유를 밝힌다)."""
    return SimpleError("workspace_untrusted", f"작업 공간의 {path}을(를) 쓸 수 없습니다: {detail}.",
                       "그 경로가 무엇인지 확인해 본인 소유의 폴더로 바꾸거나(링크라면 링크만 지우고) 다시 실행하세요. "
                       "작업 공간을 다른 디스크에 두려면 링크 대신 REPROOF_HOME이나 --workspace로 위치를 지정하세요.")


def make_private_directory(path: Path) -> Path:
    """path를 0700 폴더로 만들거나, 이미 있는 내 소유의 실제 폴더를 0700으로 좁힌 뒤 돌려준다(fail closed).

    왜 멈추나: 도구 폴더가 링크면 그 너머(작업 공간 밖)에 소스 사본·로그를 쓰게 되고, 파일이거나 다른 사용자의 폴더면
    그 사람이 내용을 읽거나 바꿀 수 있다. 그런 경우 조용히 쓰지 않고 workspace_untrusted로 멈춘다.
    """
    with contextlib.suppress(FileExistsError):  # 이미 있으면(링크·파일 포함) 아래에서 종류·소유자를 확인한다.
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise _untrusted(path, "심볼릭 링크입니다")
    if not stat.S_ISDIR(info.st_mode):
        raise _untrusted(path, "폴더가 아닙니다")
    if info.st_uid != _current_uid():
        raise _untrusted(path, "다른 사용자의 폴더입니다")
    os.chmod(path, 0o700)
    return path


def _check_workspace_root(workspace_root: Path) -> None:
    """작업 공간 루트(링크면 대상)가 내 소유의 폴더인지 보고, 그룹·다른 사용자 쓰기가 열려 있으면 0700으로 좁힌다.

    왜 루트는 링크를 허용하나: 사용자가 작업 공간을 다른 디스크로 옮기려고 ~/.reproof를 링크로 둘 수 있다. 대신 대상이
    남의 폴더면 거절한다. 쓰기가 막힌 기존 권한(예: 0755)은 사용자가 정한 것이라 바꾸지 않는다.
    """
    info = os.stat(workspace_root)
    if not stat.S_ISDIR(info.st_mode):
        raise _untrusted(workspace_root, "폴더가 아닙니다")
    if info.st_uid != _current_uid():
        raise _untrusted(workspace_root, "다른 사용자의 폴더입니다")
    if info.st_mode & 0o022:
        os.chmod(workspace_root, 0o700)


def prepare_workspace(workspace_root: Path) -> None:
    """작업 공간을 준비한다: 없으면 0700으로 만들고, 루트를 확인한 뒤 도구 폴더(TOOL_DIRECTORIES)를 만들거나 0700으로 좁힌다.

    왜: 빌드 폴더에는 회사 앱 소스 전체 사본·dirty.patch·화면 계층이, 실행기 캐시에는 비밀 값을 받는 바이너리가 있어
    같은 Mac의 다른 계정이 읽거나 바꾸면 안 된다. 주의: 믿을 수 없는 폴더는 workspace_untrusted(fail closed).
    """
    if not os.path.lexists(workspace_root):
        workspace_root.mkdir(mode=0o700, parents=True)
    _check_workspace_root(workspace_root)
    for name in TOOL_DIRECTORIES:
        make_private_directory(workspace_root / name)


def _remove_tree(path: Path) -> None:
    """읽기 전용 폴더가 있어도 트리를 지운다. 왜: 작업 사본은 원본의 권한 비트를 보존해 그대로는 못 지울 수 있다."""
    if path.exists():
        _make_tree_writable(path)
        shutil.rmtree(path)


def _remove_stale_staging(project_dir: Path) -> None:
    """이전에 중단된 실행이 남긴 staging-*.partial 폴더를 지운다(project_dir 바로 아래, 이 이름 패턴만)."""
    if project_dir.is_dir():
        for stale in project_dir.glob("staging-*.partial"):
            _remove_tree(stale)


def _relative_document(products: BuildProducts, directory: Path) -> dict:
    """products.json 내용을 만든다. 경로는 빌드 폴더 기준 상대 경로여야 폴더를 옮겨도(rename) 유효하다."""
    return {"app_path": products.app_path.relative_to(directory).as_posix(), "bundle_id": products.bundle_id,
            "derived_data": products.derived_data.relative_to(directory).as_posix(),
            "products_digest": products.products_digest}


def _inside_build(directory: Path, value: object) -> Path | None:
    """products.json의 경로 값이 빌드 폴더 안을 가리키는 상대 경로면 그 경로를, 아니면 None을 돌려준다.

    절대 경로·'..'·빈 값은 거절하고, 링크를 풀어도(realpath) 빌드 폴더 안이어야 한다.
    """
    if not isinstance(value, str) or not value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        return None
    path = directory / value
    is_inside = Path(os.path.realpath(path)).is_relative_to(os.path.realpath(directory))
    return path if is_inside else None


def _read_products(directory: Path) -> BuildProducts | None:
    """products.json을 읽어 BuildProducts로 만든다. 파일이 없거나 깨졌거나 경로가 빌드 폴더 밖이면 None."""
    try:
        document = json.loads((directory / PRODUCTS_FILE).read_text(encoding="utf-8"))
        app_path = _inside_build(directory, document["app_path"])
        derived_data = _inside_build(directory, document["derived_data"])
        bundle_id, digest = document["bundle_id"], document["products_digest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None  # 완성 표식이 없거나 깨졌으면 완성된 빌드가 아니므로 호출자가 새로 만든다.
    if app_path is None or derived_data is None:
        return None
    return BuildProducts(app_path, bundle_id, derived_data, digest)


def _matches_record(products: BuildProducts, config: ProjectConfig) -> bool:
    """기록과 실제 앱이 같은지 본다: bundle id(기록·Info.plist)가 설정과 같고 .app 트리 digest가 기록과 같아야 한다.

    왜: 작업 공간의 .app이 바뀌었으면(다른 도구·사람의 수정, 깨진 파일) 같은 build_id로 다른 바이너리를 설치하게 된다.
    digest를 계산하지 못하면(읽기 오류·특수 파일) 같다고 볼 근거가 없으므로 False로 보고 호출자가 다시 빌드한다.
    """
    if products.bundle_id != config.bundle_id or not products.app_path.is_dir():
        return False
    if _bundle_identifier(products.app_path) != config.bundle_id:
        return False
    try:
        return tree_digest(products.app_path) == products.products_digest
    except (OSError, SimpleError):
        return False  # 확인할 수 없는 산출물은 재사용하지 않고 새로 빌드한다(오류는 새 빌드에서 다시 드러난다).


def _load_completed(directory: Path, identity: BuildIdentity, build_id: str,
                    config: ProjectConfig) -> PreparedBuild | None:
    """완성된 빌드 폴더를 검증해 PreparedBuild로 돌려준다. 완성이 아니거나 기록과 다르면 None(호출자가 지우고 다시 빌드)."""
    products = _read_products(directory)
    if products is None or not _matches_record(products, config):
        return None
    return PreparedBuild(build_id, identity, _copy_info(directory, identity), products, directory)


def _copy_info(directory: Path, identity: BuildIdentity) -> WorkingCopy:
    """완성 폴더의 source를 가리키는 WorkingCopy를 만든다. 개수·크기는 재사용 시 다시 세지 않아 0이다."""
    return WorkingCopy(directory / "source", identity.source_manifest_digest, 0, 0, identity.excluded_paths)


def _rebase(prepared_source: WorkingCopy, products: BuildProducts, partial: Path, final: Path):
    """partial 아래 경로를 final 아래로 바꾼 (WorkingCopy, BuildProducts)를 돌려준다."""
    def move(path: Path) -> Path:
        return final / path.relative_to(partial)
    working_copy = WorkingCopy(move(prepared_source.root), prepared_source.manifest_digest,
                               prepared_source.file_count, prepared_source.total_bytes, prepared_source.excluded_paths)
    return working_copy, BuildProducts(move(products.app_path), products.bundle_id, move(products.derived_data),
                                       products.products_digest)


def _run_build_steps(partial: Path, working_copy: WorkingCopy, config: ProjectConfig, cache_root: Path,
                     runner: ToolRunner, progress: Callable[[str], None]) -> BuildProducts:
    """생성 → 패키지 → 빌드를 partial 폴더 안에서 실행한다. 진행 메시지는 고정 순서로 낸다.

    패키지 해석과 빌드는 공유 패키지 캐시 잠금(package_cache_lock) 안에서 한다. 왜: 같은 캐시를 쓰는 다른 프로젝트의
    실행이 체크아웃을 동시에 바꾸지 못하게 하려는 것이다(빌드도 그 체크아웃을 읽는다).
    """
    logs = partial / "logs"
    logs.mkdir(exist_ok=True)
    if config.generator != "none":
        progress("프로젝트 생성 중")
        generate_project(working_copy.root, config, runner, log_path=logs / "generate.log")
    with package_cache_lock(package_cache_dir(working_copy.root, config, cache_root)):
        progress("패키지 받는 중")
        derived_data = partial / "DerivedData"  # 패키지 해석과 빌드가 같은 폴더를 써 전역 DerivedData를 건드리지 않는다.
        packages = resolve_packages(working_copy.root, config, cache_root=cache_root, runner=runner,
                                    log_path=logs / "packages.log", warn=progress, derived_data=derived_data)
        progress("앱 빌드 중")
        return build_for_simulator(working_copy.root, config, derived_data=derived_data, packages=packages,
                                   runner=runner, log_path=logs / "build.log")


def _stage_working_copy(source_root: Path, project_dir: Path, config: ProjectConfig, progress: Callable[[str], None]):
    """staging 폴더에 작업 사본을 만든다. build_id는 사본의 manifest digest가 있어야 정해지기 때문이다.

    비밀 가능 파일은 빼고(설정의 [source] include는 넣는다) 뺀 파일마다 progress로 알린다.
    """
    _remove_stale_staging(project_dir)
    progress("작업 사본 만드는 중")
    staging = (project_dir / f"staging-{uuid.uuid4().hex}.partial").absolute()
    try:
        return staging, materialize_working_copy(source_root, staging / "source",
                                                 include_paths=frozenset(config.source_include), notice=progress)
    except BaseException:
        _remove_tree(staging)
        raise


def prepare_build(source_root: Path, config: ProjectConfig, *, workspace_root: Path, runner: ToolRunner,
                  progress: Callable[[str], None] = print) -> PreparedBuild:
    """소스를 식별하고 Simulator용 Debug 빌드를 만든다. 같은 build_id의 완성 빌드가 있으면 재사용한다.

    build_id에는 소스·설정 지문과 함께 빌드 방법 버전(BUILD_RECIPE_VERSION)과 Xcode 버전이 들어간다.

    흐름: 작업 사본을 staging에 만들고 build_id를 정한 뒤 `<build_id>.partial/`에서 작업하고,
    성공해야 `<build_id>/`로 rename한다. 도중에 실패·중단되면 partial을 남기고 예외를 그대로 던지며,
    다음 실행이 partial을 지우고 처음부터 다시 한다(끊긴 빌드를 완성으로 착각하지 않으려는 것).
    잠금: Xcode 버전 확인 뒤의 모든 작업 공간 변경을 프로젝트 잠금(project_lock) 안에서 한다. 왜: 다른 터미널의 같은
    프로젝트 실행이 쓰고 있는 staging·partial을 "남은 잔재"로 보고 지우지 않게 하려는 것이다. 잠겨 있으면 작업 공간을
    바꾸지 않고 project_busy를 낸다.
    """
    workspace_root = workspace_root.expanduser().absolute()  # xcodebuild는 cwd 기준으로 경로를 풀기 때문
    progress("빌드 식별 중")
    xcode_version = read_xcode_version(runner)  # 복사·잠금 전에 확인해 Xcode가 없으면 작업 공간을 건드리지 않고 멈춘다.
    prepare_workspace(workspace_root)
    with project_lock(workspace_root, config.name):  # 이름 검사가 폴더를 만들기 전에 돌도록 잠금부터 잡는다.
        make_private_directory(workspace_root / "projects" / config.name)
        return _prepare_locked(source_root, config, workspace_root, runner, progress, xcode_version)


def _prepare_locked(source_root: Path, config: ProjectConfig, workspace_root: Path, runner: ToolRunner,
                    progress: Callable[[str], None], xcode_version: str) -> PreparedBuild:
    """프로젝트 잠금을 쥔 상태에서 식별 → 작업 사본 → (재사용 또는) 빌드를 한다. 흐름은 prepare_build 설명과 같다."""
    project_dir = workspace_root / "projects" / config.name
    builds_dir = make_private_directory(project_dir / "builds")
    staging, copy = _stage_working_copy(source_root, project_dir, config, progress)
    try:
        identity = capture_build_identity(source_root, config_digest=config_digest(config),
                                          source_manifest_digest=copy.manifest_digest, output_dir=staging,
                                          runner=runner, build_recipe=BUILD_RECIPE_VERSION,
                                          xcode_version=xcode_version, excluded_paths=copy.excluded_paths)
        build_id = identity.build_id()
        final, partial = builds_dir / build_id, builds_dir / f"{build_id}.partial"
        cached = _load_completed(final, identity, build_id, config)
        if cached is not None:
            _remove_tree(staging)
            progress(f"빌드 완료: {cached.products.app_path}")
            return cached
        _remove_tree(partial)
        _remove_tree(final)  # 완성 표식이 없는 폴더는 이전 실행의 잔재이므로 rename 전에 치운다.
        staging.rename(partial)
    except BaseException:
        _remove_tree(staging)
        raise
    return _finish_build(partial, final, build_id, identity, copy, config, workspace_root, runner, progress)


def _finish_build(partial: Path, final: Path, build_id: str, identity: BuildIdentity, copy: WorkingCopy,
                  config: ProjectConfig, workspace_root: Path, runner: ToolRunner,
                  progress: Callable[[str], None]) -> PreparedBuild:
    """partial에서 빌드하고 기록 파일을 쓴 뒤 final로 rename한다. 실패하면 partial을 남기고 예외를 그대로 던진다.

    작업 사본에서 비밀 가능 파일을 뺐으면 생성·패키지·빌드 실패 안내에 [source] include 안내를 덧붙인다.
    """
    working_copy = WorkingCopy(partial / "source", copy.manifest_digest, copy.file_count, copy.total_bytes,
                               copy.excluded_paths)
    try:
        products = _run_build_steps(partial, working_copy, config, workspace_root / "cache", runner, progress)
    except SimpleError as error:
        add_include_hint(error, copy.excluded_paths)  # 빠진 비밀 가능 파일이 실패 원인일 수 있다.
        raise
    (partial / "identity.json").write_text(json.dumps(identity.to_document(), ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    (partial / PRODUCTS_FILE).write_text(json.dumps(_relative_document(products, partial), ensure_ascii=False,
                                                    indent=2), encoding="utf-8")
    partial.rename(final)
    final_copy, final_products = _rebase(working_copy, products, partial, final)
    progress(f"빌드 완료: {final_products.app_path}")
    return PreparedBuild(build_id, identity, final_copy, final_products, final)
