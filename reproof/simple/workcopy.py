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

    def __init__(self, limits: CopyLimits | None, *, is_copy: bool):
        """is_copy가 True면 제외 규칙·링크 검증·상한 검사를 적용하고 디렉터리 권한을 모은다.

        tree_digest는 is_copy=False(limits=None)로 만들어 사본을 있는 그대로 해시한다.
        """
        self.limits = limits
        self.is_copy = is_copy
        self.copy_root: Path | None = None
        """복사 중일 때 사본 루트. 링크를 사본 안으로 다시 계산할 때 쓴다."""
        self.links: list[Path] = []
        """복사 중에 사본에 만든 링크들. 복사가 끝난 뒤 사본 안으로 풀리는지 한꺼번에 확인한다."""
        self.directory_modes: list[tuple[Path, int]] = []
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


def _is_inside(path: Path, root: Path) -> bool:
    """path가 root 자신이거나 root 아래인지 경로 구성요소로 판단한다(문자열 접두사 비교의 '/a' vs '/ab' 오류 방지)."""
    return path == root or root in path.parents


def _outside_link_error(relative: str, target_text: str) -> SimpleError:
    """앱 소스(또는 작업 사본) 밖을 가리키는 링크를 알리는 source_symlink_outside 오류를 만든다."""
    return SimpleError(
        "source_symlink_outside",
        f"심볼릭 링크 '{relative}'가 앱 소스 밖({target_text})을 가리킵니다.",
        "링크를 소스 폴더 안의 실제 파일로 바꾸거나 제거한 뒤 다시 실행하세요.",
    )


def _symlink_text_in_copy(link: Path, source_root: Path, copy_link: Path, copy_root: Path) -> str:
    """원본 링크가 가리키는 곳을 사본 안의 같은 위치로 옮긴, 사본 링크 위치 기준 상대 링크 문자열을 돌려준다.

    link: 원본의 링크. copy_link: 사본에 만들 링크 경로. copy_root: 사본 루트(source_root에 대응).
    왜 원문을 그대로 쓰지 않나: '../'를 충분히 쓴 상대 링크는 원본 절대 경로로 돌아갈 수 있어, 사본에서도 원본을
    가리킨다. 해석한 대상(realpath)을 source_root 기준으로 사본에 옮겨 적으면 사본은 항상 사본 안을 가리킨다.
    반환: 링크 문자열. 주의: 해석한 대상이 소스 밖이면 source_symlink_outside.
    """
    resolved = Path(os.path.realpath(link))
    if not _is_inside(resolved, source_root):
        raise _outside_link_error(str(link.relative_to(source_root)), os.readlink(link))
    return os.path.relpath(copy_root / resolved.relative_to(source_root), copy_link.parent)


def _verify_links_inside_copy(links: list[Path], copy_root: Path) -> None:
    """사본에 만든 링크가 모두(다른 링크를 거쳐도) 사본 안으로 풀리는지 확인한다. 하나라도 밖이면 source_symlink_outside.

    왜: 링크 문자열을 계산으로 만들었더라도, 모든 항목이 생긴 뒤의 실제 해석 결과로 한 번 더 확인해 원본 저장소를
    가리키는 링크가 남지 않게 하려는 것이다.
    """
    real_root = Path(os.path.realpath(copy_root))
    for link in links:
        if not _is_inside(Path(os.path.realpath(link)), real_root):
            raise _outside_link_error(str(link.relative_to(copy_root)), os.readlink(link))


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
            if manifest.is_copy and _is_excluded(entry.name, current == root):
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
            manifest.directory_modes.append((child_target, stat.S_IMODE(mode)))
        stack.append((path, child_target))
    elif stat.S_ISLNK(mode):
        _visit_link(path, relative, child_target, manifest, source_root)
    elif stat.S_ISREG(mode):
        _visit_regular_file(path, relative, mode, child_target, manifest)
    else:
        _reject_special_file(path, root)


def _visit_link(path: Path, relative: str, child_target: Path | None, manifest: _Manifest, source_root: Path) -> None:
    """링크 하나를 기록한다. 복사 중이면 사본 안을 가리키도록 다시 계산한 링크를 만들고 나중 검증 목록에 넣는다."""
    if child_target is None:
        link_text = os.readlink(path)
    else:
        link_text = _symlink_text_in_copy(path, source_root, child_target, manifest.copy_root)
        os.symlink(link_text, child_target)
        manifest.links.append(child_target)
    manifest.add(relative, "link", 0, link_text)
    manifest.count_file(0)


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
    manifest = _Manifest(limits, is_copy=True)
    manifest.copy_root = destination
    destination.mkdir(parents=True)
    manifest.directory_modes.append((destination, stat.S_IMODE(os.stat(source_root).st_mode)))
    try:
        _walk(source_root, destination, source_root, manifest, source_root)
        _verify_links_inside_copy(manifest.links, destination)
        _apply_directory_modes(manifest.directory_modes)
    except BaseException as error:
        _discard_partial_copy(destination, error)
        raise
    return WorkingCopy(destination, manifest.digest(), manifest.file_count, manifest.total_bytes)


def _apply_directory_modes(directory_modes: list[tuple[Path, int]]) -> None:
    """디렉터리 권한을 가장 깊은 것부터 적용한다.

    왜: 읽기 전용(0o555) 소스 디렉터리를 만들 때 먼저 권한을 맞추면 그 안에 파일을 쓸 수 없다.
    자식을 먼저 처리해야 부모가 잠겨도 자식 chmod가 막히지 않는다.
    """
    for directory, mode in sorted(directory_modes, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(directory, mode)


def _make_tree_writable(root: Path) -> None:
    """삭제가 가능하도록 root 아래 모든 디렉터리에 u+rwx를 복구한다(링크는 따라가지 않는다)."""
    stack = [root]
    while stack:
        directory = stack.pop()
        os.chmod(directory, stat.S_IMODE(os.stat(directory).st_mode) | 0o700)
        with os.scandir(directory) as entries:
            stack.extend(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))


def _discard_partial_copy(destination: Path, original: BaseException) -> None:
    """실패한 복사가 남긴 destination을 지운다. 지우지 못하면 원래 오류에 남은 경로를 알린다.

    왜: 반쯤 복사된 사본이 남으면 다음 실행이 이유 없이 destination_exists로 실패한다.
    정리 실패를 삼키지 않고 원래 오류(SimpleError는 remedy, 그 외는 note)에 덧붙여 원인을 보존한다.
    """
    try:
        _make_tree_writable(destination)
        shutil.rmtree(destination)
    except OSError as cleanup_error:
        message = f"실패한 작업 사본 '{destination}'를 정리하지 못했습니다({cleanup_error}). 직접 삭제한 뒤 다시 실행하세요."
        original.add_note(message)
        if isinstance(original, SimpleError):
            original.remedy = f"{original.remedy} 또한 {message}"


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
    manifest = _Manifest(None, is_copy=False)
    resolved = Path(os.path.realpath(root))
    _walk(resolved, None, resolved, manifest, resolved)
    return manifest.digest()
