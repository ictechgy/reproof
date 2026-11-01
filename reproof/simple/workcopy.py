"""앱 소스 작업 사본 생성과 매니페스트 digest.

왜: 원본 소스를 건드리지 않고 빌드하려고 복사본을 만든다. 실무 앱은 파일이 수천 개이고
에셋·스크립트가 섞여 있으므로 빌드·VCS 부산물만 빼고 전부 복사하며, 실행 권한을 보존하고,
복사본이 원본과 같은지 확인할 수 있는 안정적인 digest를 남긴다.
"""
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from reproof.simple.errors import SimpleError

CHUNK_BYTES = 1024 * 1024

# 어느 깊이에서든 제외하는 이름. 빌드 산출물·VCS·사용자별 Xcode 상태라 소스가 아니다.
EXCLUDED_NAMES = frozenset({".git", "DerivedData", ".build", "xcuserdata", ".reproof"})
# 루트 바로 아래에서만 제외하는 이름. 하위의 build는 소스 폴더일 수 있어 남긴다.
ROOT_ONLY_EXCLUDED_NAMES = frozenset({"build"})


@dataclass(frozen=True)
class CopyLimits:
    """복사 상한. 잘못 지정한 거대한 폴더(홈 디렉터리 등)를 통째로 복사하는 사고를 막는다."""

    max_files: int = 100_000
    max_total_bytes: int = 4 * 1024**3
    max_file_bytes: int = 1024**3


@dataclass(frozen=True)
class WorkingCopy:
    """만들어진 작업 사본. root: 사본 경로, manifest_digest: 매니페스트 sha256,
    file_count: 파일·링크 수(디렉터리 제외), total_bytes: 일반 파일 크기 합."""

    root: Path
    manifest_digest: str
    file_count: int
    total_bytes: int


class _Manifest:
    """매니페스트 줄과 합계를 모은다. 복사와 tree_digest가 같은 규칙을 쓰게 하려는 공용 누적기."""

    def __init__(self, limits: CopyLimits | None):
        """limits가 있으면 추가할 때마다 상한을 검사한다(tree_digest는 None)."""
        self.limits = limits
        self.lines: list[tuple[bytes, str]] = []
        self.file_count = 0
        self.total_bytes = 0

    def add(self, relative: str, kind: str, executable: int, value: str) -> None:
        """정렬 키(경로 바이트)와 함께 한 줄을 기록한다."""
        self.lines.append((os.fsencode(relative), f"{relative}\0{kind}\0{executable:o}\0{value}\n"))

    def count_file(self, size: int) -> None:
        """파일 하나를 합계에 반영하고 상한을 검사한다."""
        self.file_count += 1
        self.total_bytes += size
        if self.limits is None:
            return
        _check_limit("파일 수", self.file_count, self.limits.max_files, "개")
        _check_limit("전체 크기", self.total_bytes, self.limits.max_total_bytes, " 바이트")

    def digest(self) -> str:
        """경로 순으로 정렬한 줄들의 sha256을 돌려준다."""
        joined = "".join(line for _, line in sorted(self.lines))
        return hashlib.sha256(joined.encode("utf-8", "surrogateescape")).hexdigest()


def _check_limit(label: str, actual: int, limit: int, unit: str) -> None:
    """actual이 limit를 넘으면 한도와 실제 값을 밝힌 source_too_large 오류를 낸다."""
    if actual > limit:
        raise SimpleError(
            "source_too_large",
            f"앱 소스의 {label}가 한도({limit:,}{unit})를 넘었습니다(현재 {actual:,}{unit} 이상).",
            "앱 프로젝트 폴더를 정확히 지정했는지 확인하고, 소스가 아닌 큰 파일은 폴더 밖으로 옮긴 뒤 다시 실행하세요.",
        )


def _is_excluded(name: str, is_root: bool) -> bool:
    """이름 규칙에 따라 복사에서 제외할 항목인지 판단한다."""
    return name in EXCLUDED_NAMES or (is_root and name in ROOT_ONLY_EXCLUDED_NAMES)


def _symlink_target_inside(link: Path, source_root: Path) -> str:
    """링크가 source 안을 가리키면 사본에 쓸 링크 문자열을, 밖이면 오류를 돌려준다.

    상대 링크는 원문 그대로, 절대 링크는 사본에서도 유효하도록 링크 위치 기준 상대 경로로 바꾼다.
    """
    target_text = os.readlink(link)
    resolved = Path(os.path.realpath(link))
    if resolved != source_root and source_root not in resolved.parents:
        raise SimpleError(
            "source_symlink_outside",
            f"심볼릭 링크 '{link.relative_to(source_root)}'가 앱 소스 밖({target_text})을 가리킵니다.",
            "링크를 소스 폴더 안의 실제 파일로 바꾸거나 제거한 뒤 다시 실행하세요.",
        )
    if os.path.isabs(target_text):
        return os.path.relpath(resolved, os.path.realpath(link.parent))
    return target_text


def _reject_special_file(path: Path, root: Path) -> None:
    """장치·FIFO·소켓은 복사할 수 없으므로 source_special_file 오류를 낸다."""
    raise SimpleError(
        "source_special_file",
        f"'{path.relative_to(root)}'는 일반 파일이 아닌 특수 파일(장치·FIFO·소켓)입니다.",
        "해당 파일을 소스 폴더에서 제거한 뒤 다시 실행하세요.",
    )


def _copy_file(source_file: Path, destination_file: Path | None, max_file_bytes: int | None) -> tuple[str, int]:
    """파일을 1 MiB씩 읽으며 sha256을 계산하고, destination_file이 있으면 함께 쓴다.

    반환: (hex digest, 바이트 수). 크기 상한은 읽는 도중에도 검사해 stat 이후 커진 파일도 막는다.
    """
    hasher, size = hashlib.sha256(), 0
    sink = open(destination_file, "wb") if destination_file else None
    try:
        with open(source_file, "rb") as reader:
            while chunk := reader.read(CHUNK_BYTES):
                size += len(chunk)
                if max_file_bytes is not None:
                    _check_limit(f"파일 '{source_file.name}'의 크기", size, max_file_bytes, " 바이트")
                hasher.update(chunk)
                if sink:
                    sink.write(chunk)
    finally:
        if sink:
            sink.close()
    if destination_file:
        shutil.copymode(source_file, destination_file)
    return hasher.hexdigest(), size


def _walk(directory: Path, destination: Path | None, root: Path, manifest: _Manifest, source_root: Path) -> None:
    """directory를 반복(스택) 순회하며 매니페스트를 채우고, destination이 있으면 복사한다.

    깊은 트리에서 재귀 한도에 걸리지 않도록 스택을 쓴다. 제외 규칙은 복사할 때만 적용한다.
    """
    stack = [(directory, destination)]
    while stack:
        current, target = stack.pop()
        with os.scandir(current) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            if manifest.limits is not None and _is_excluded(entry.name, current == root):
                continue
            child_target = target / entry.name if target else None
            _visit(entry, child_target, root, manifest, source_root, stack)


def _visit(entry: os.DirEntry, child_target: Path | None, root: Path, manifest: _Manifest,
           source_root: Path, stack: list) -> None:
    """항목 하나(디렉터리·링크·일반 파일·특수 파일)를 종류별로 처리한다."""
    path, relative = Path(entry.path), os.path.relpath(entry.path, root)
    mode = os.lstat(path).st_mode
    if stat.S_ISDIR(mode):
        manifest.add(relative, "dir", 0, "")
        if child_target:
            child_target.mkdir()
            shutil.copymode(path, child_target)
        stack.append((path, child_target))
    elif stat.S_ISLNK(mode):
        link_text = _symlink_target_inside(path, source_root) if manifest.limits else os.readlink(path)
        manifest.add(relative, "link", 0, link_text)
        manifest.count_file(0)
        if child_target:
            os.symlink(link_text, child_target)
    elif stat.S_ISREG(mode):
        _visit_regular_file(path, relative, mode, child_target, manifest)
    else:
        _reject_special_file(path, root)


def _visit_regular_file(path: Path, relative: str, mode: int, child_target: Path | None, manifest: _Manifest) -> None:
    """일반 파일의 크기 상한을 검사하고 복사·해시한 뒤 매니페스트에 기록한다."""
    max_file = manifest.limits.max_file_bytes if manifest.limits else None
    if max_file is not None:
        _check_limit(f"파일 '{relative}'의 크기", os.lstat(path).st_size, max_file, " 바이트")
    content_hash, size = _copy_file(path, child_target, max_file)
    manifest.add(relative, "file", mode & 0o111, content_hash)
    manifest.count_file(size)


def materialize_working_copy(source: Path, destination: Path, limits: CopyLimits = CopyLimits()) -> WorkingCopy:
    """source를 destination으로 복사한다(빌드·VCS 부산물 제외, 실행 권한 보존).

    destination은 존재하면 안 된다(남은 이전 결과를 섞어 쓰지 않으려는 것). 실패하면 만들다 만
    destination을 지워 반쯤 복사된 사본이 재사용되지 않게 하고 오류는 그대로 다시 던진다.
    반환: WorkingCopy. 주의: 소스 밖을 가리키는 링크·특수 파일·상한 초과는 SimpleError.
    """
    source_root = Path(os.path.realpath(source))
    _validate_paths(source_root, destination)
    manifest = _Manifest(limits)
    destination.mkdir(parents=True)
    try:
        _walk(source_root, destination, source_root, manifest, source_root)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return WorkingCopy(destination, manifest.digest(), manifest.file_count, manifest.total_bytes)


def _validate_paths(source_root: Path, destination: Path) -> None:
    """source가 디렉터리이고 destination이 없으며 source 안이 아닌지 확인한다."""
    if not source_root.is_dir():
        raise SimpleError("source_missing", f"앱 소스 폴더를 찾을 수 없습니다: {source_root}",
                          "앱 프로젝트 폴더 경로를 확인한 뒤 다시 실행하세요.")
    if os.path.lexists(destination):
        raise SimpleError("destination_exists", f"작업 사본 위치가 이미 있습니다: {destination}",
                          "기존 폴더를 지우거나 다른 위치를 지정한 뒤 다시 실행하세요.")
    resolved = Path(os.path.realpath(destination))
    if resolved == source_root or source_root in resolved.parents:
        raise SimpleError("destination_inside_source", "작업 사본 위치가 앱 소스 폴더 안에 있습니다.",
                          "소스 폴더 밖의 위치를 지정한 뒤 다시 실행하세요.")


def tree_digest(root: Path) -> str:
    """root 트리의 매니페스트 digest를 계산한다(제외·상한 없음, 사본 검증용).

    정렬된 (상대경로, 종류, 권한비트 & 0o111, 내용 sha256 또는 링크 대상) 줄의 sha256이며
    materialize_working_copy의 manifest_digest와 같은 규칙이다.
    """
    manifest = _Manifest(None)
    resolved = Path(os.path.realpath(root))
    _walk(resolved, None, resolved, manifest, resolved)
    return manifest.digest()
