"""빌드 파이프라인: 식별 → 작업 사본 → (생성) → 패키지 → Debug 빌드. 끊긴 빌드는 이어 쓰지 않고 다시 시작한다."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .build import BUILD_RECIPE_VERSION, BuildProducts, build_for_simulator, read_xcode_version
from .config import ProjectConfig, config_digest
from .generation import generate_project
from .identity import BuildIdentity, capture_build_identity
from .packages import resolve_packages
from .process import ToolRunner
from .workcopy import WorkingCopy, _make_tree_writable, materialize_working_copy

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


def _load_completed(directory: Path, identity: BuildIdentity, build_id: str) -> PreparedBuild | None:
    """완성된 빌드 폴더를 읽어 PreparedBuild로 돌려준다. 완성이 아니면(파일 없음·깨짐·앱 없음) None."""
    try:
        document = json.loads((directory / PRODUCTS_FILE).read_text(encoding="utf-8"))
        products = BuildProducts(directory / document["app_path"], document["bundle_id"],
                                 directory / document["derived_data"], document["products_digest"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # 완성 표식이 없거나 깨졌으면 완성된 빌드가 아니므로 호출자가 새로 만든다.
    if not products.app_path.exists():
        return None
    return PreparedBuild(build_id, identity, _copy_info(directory, identity), products, directory)


def _copy_info(directory: Path, identity: BuildIdentity) -> WorkingCopy:
    """완성 폴더의 source를 가리키는 WorkingCopy를 만든다. 개수·크기는 재사용 시 다시 세지 않아 0이다."""
    return WorkingCopy(directory / "source", identity.source_manifest_digest, 0, 0)


def _rebase(prepared_source: WorkingCopy, products: BuildProducts, partial: Path, final: Path):
    """partial 아래 경로를 final 아래로 바꾼 (WorkingCopy, BuildProducts)를 돌려준다."""
    def move(path: Path) -> Path:
        return final / path.relative_to(partial)
    working_copy = WorkingCopy(move(prepared_source.root), prepared_source.manifest_digest,
                               prepared_source.file_count, prepared_source.total_bytes)
    return working_copy, BuildProducts(move(products.app_path), products.bundle_id, move(products.derived_data),
                                       products.products_digest)


def _run_build_steps(partial: Path, working_copy: WorkingCopy, config: ProjectConfig, cache_root: Path,
                     runner: ToolRunner, progress: Callable[[str], None]) -> BuildProducts:
    """생성 → 패키지 → 빌드를 partial 폴더 안에서 실행한다. 진행 메시지는 고정 순서로 낸다."""
    logs = partial / "logs"
    logs.mkdir(exist_ok=True)
    if config.generator != "none":
        progress("프로젝트 생성 중")
        generate_project(working_copy.root, config, runner, log_path=logs / "generate.log")
    progress("패키지 받는 중")
    packages = resolve_packages(working_copy.root, config, cache_root=cache_root, runner=runner,
                                log_path=logs / "packages.log", warn=progress)
    progress("앱 빌드 중")
    return build_for_simulator(working_copy.root, config, derived_data=partial / "DerivedData", packages=packages,
                               runner=runner, log_path=logs / "build.log")


def _stage_working_copy(source_root: Path, project_dir: Path, progress: Callable[[str], None]):
    """staging 폴더에 작업 사본을 만든다. build_id는 사본의 manifest digest가 있어야 정해지기 때문이다."""
    _remove_stale_staging(project_dir)
    progress("작업 사본 만드는 중")
    staging = (project_dir / f"staging-{uuid.uuid4().hex}.partial").absolute()
    try:
        return staging, materialize_working_copy(source_root, staging / "source")
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
    """
    workspace_root = workspace_root.expanduser().absolute()  # xcodebuild는 cwd 기준으로 경로를 풀기 때문
    progress("빌드 식별 중")
    xcode_version = read_xcode_version(runner)  # 복사 전에 확인해 Xcode가 없으면 바로 멈춘다.
    project_dir = workspace_root / "projects" / config.name
    builds_dir = project_dir / "builds"
    staging, copy = _stage_working_copy(source_root, project_dir, progress)
    try:
        identity = capture_build_identity(source_root, config_digest=config_digest(config),
                                          source_manifest_digest=copy.manifest_digest, output_dir=staging,
                                          runner=runner, build_recipe=BUILD_RECIPE_VERSION,
                                          xcode_version=xcode_version)
        build_id = identity.build_id()
        final, partial = builds_dir / build_id, builds_dir / f"{build_id}.partial"
        cached = _load_completed(final, identity, build_id)
        if cached is not None:
            _remove_tree(staging)
            progress(f"빌드 완료: {cached.products.app_path}")
            return cached
        builds_dir.mkdir(parents=True, exist_ok=True)
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
    """partial에서 빌드하고 기록 파일을 쓴 뒤 final로 rename한다. 실패하면 partial을 남기고 예외를 그대로 던진다."""
    working_copy = WorkingCopy(partial / "source", copy.manifest_digest, copy.file_count, copy.total_bytes)
    products = _run_build_steps(partial, working_copy, config, workspace_root / "cache", runner, progress)
    (partial / "identity.json").write_text(json.dumps(identity.to_document(), ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    (partial / PRODUCTS_FILE).write_text(json.dumps(_relative_document(products, partial), ensure_ascii=False,
                                                    indent=2), encoding="utf-8")
    partial.rename(final)
    final_copy, final_products = _rebase(working_copy, products, partial, final)
    progress(f"빌드 완료: {final_products.app_path}")
    return PreparedBuild(build_id, identity, final_copy, final_products, final)
