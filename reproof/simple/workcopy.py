"""앱 소스 작업 사본 생성과 매니페스트 digest.

왜: 원본 소스를 건드리지 않고 빌드하려고 복사본을 만든다. 실무 앱은 파일이 수천 개이고
에셋·스크립트가 섞여 있으므로 빌드·VCS 부산물만 빼고 전부 복사하며, 실행 권한을 보존하고,
복사본이 원본과 같은지 확인할 수 있는 안정적인 digest를 남긴다.
"""
import contextlib
import errno
import fcntl
import fnmatch
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

from reproof.simple.errors import SimpleError

CHUNK_BYTES = 1024 * 1024
_PERMISSION_BITS = 0o777
"""사본에 옮기는 권한 비트(rwx). setuid·setgid·sticky는 옮기지 않는다."""

# 어느 깊이에서든 제외하는 이름. 빌드 산출물·VCS·사용자별 Xcode 상태라 소스가 아니다.
EXCLUDED_NAMES = frozenset({".git", "DerivedData", ".build", "xcuserdata", ".reproof"})
# 루트 바로 아래에서만 제외하는 이름. 하위의 build는 소스 폴더일 수 있어 남긴다.
ROOT_ONLY_EXCLUDED_NAMES = frozenset({"build"})

CREDENTIAL_NAME_PATTERNS = (
    ".env", ".env.*", ".git-credentials", ".netrc", ".npmrc", "*.p8", "*.p12", "*.pfx", "*.pem", "*.key",
    "*.keystore", "*.jks", "*.mobileprovision", "*.provisionprofile", "id_rsa*", "id_ed25519*", "*.cer",
)
"""비밀(토큰·서명 키·인증서·프로비저닝 프로필)이 들어 있을 수 있는 파일 이름(fnmatch, 소문자로 비교).

왜: 작업 사본은 작업 공간(~/.reproof)에 남고 나중 단계에서 AI가 읽을 수 있다. gitignore된 .env나 Fastlane .p8 키가
그대로 복사되면 '툴은 인증 값을 읽거나 저장하지 않는다'는 약속이 깨지므로 기본으로 열지도 않고 뺀다. 빌드에 꼭 필요한
파일은 reproof.toml `[source] include`로 경로를 지정해 넣는다. identity.py가 dirty.patch에서도 같은 이름을 뺀다.
"""


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
    excluded_paths: tuple[str, ...] = ()
    """비밀 가능 이름이라 복사하지 않은 파일·링크의 상대 경로(정렬됨). 빌드 식별에 넣어 무엇이 빠졌는지 남긴다."""


def is_credential_name(name: str) -> bool:
    """파일 이름이 CREDENTIAL_NAME_PATTERNS 중 하나와 맞으면 True. 대소문자는 구분하지 않는다(APFS 기본과 같게)."""
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in CREDENTIAL_NAME_PATTERNS)


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
        self.include_paths: Collection[str] = frozenset()
        """비밀 가능 이름이어도 복사할 상대 경로(reproof.toml [source] include)."""
        self.excluded: list[str] = []
        """복사하지 않은 비밀 가능 파일의 상대 경로."""
        self.notice: Callable[[str], None] = print
        """제외한 파일을 알리는 함수(파일마다 한 번)."""
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


def _stays_inside_lexically(link_directory: str, target_text: str) -> bool:
    """상대 링크 문자열을 링크 폴더(소스 루트 기준) 아래에서 글자로만 정리했을 때 소스 루트 안에 머무는지 본다.

    링크를 따라가지 않고 '.'·'..'·중복 '/'만 접는다(os.path.normpath). 절대 경로이거나 루트 위('..')로 나가면 False.
    """
    if os.path.isabs(target_text):
        return False
    normalized = os.path.normpath(os.path.join(link_directory, target_text))
    return normalized != ".." and not normalized.startswith("../")


def _symlink_text_in_copy(link: Path, source_root: Path, copy_link: Path, copy_root: Path) -> str:
    """사본에 만들 링크 문자열을 돌려준다. 소스 밖으로 풀리는 링크는 source_symlink_outside.

    link: 원본의 링크. copy_link: 사본에 만들 링크 경로. copy_root: 사본 루트(source_root에 대응).
    상대 링크가 글자상 소스 안에 머물면 원문을 바이트 그대로 쓴다. 왜: 서명된 framework·XCFramework는
    `Headers -> Versions/Current/Headers` 같은 링크 문자열까지 봉인하므로, 끝까지 푼 경로로 바꾸면 서명 검증이 깨진다.
    절대 링크와 글자상 소스 밖으로 나가는 상대 링크('../'를 충분히 써서 원본 절대 경로로 돌아가는 모양)만, 해석한 대상
    (realpath)을 source_root 기준으로 사본에 옮긴 상대 링크로 다시 만든다(사본이 원본을 가리키지 않게).
    마지막 안전망은 _verify_links_inside_copy다(사본의 실제 해석 결과로 다시 확인).
    """
    resolved = Path(os.path.realpath(link))
    target_text = os.readlink(link)
    if not _is_inside(resolved, source_root):
        raise _outside_link_error(str(link.relative_to(source_root)), target_text)
    if _stays_inside_lexically(os.path.relpath(link.parent, source_root), target_text):
        return target_text
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


_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
"""원본 파일을 여는 플래그. 링크는 따라가지 않고(ELOOP), 그 사이 FIFO로 바뀌었어도 쓰는 쪽을 기다리며 멈추지 않는다."""

_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
"""사본 파일을 만드는 플래그. 이미 있는 파일·링크에는 쓰지 않는다."""


def _source_changed_error(source_file: Path) -> SimpleError:
    """복사 도중 원본 항목이 바뀌었음(링크·다른 파일로 교체)을 알리는 source_changed 오류를 만든다."""
    return SimpleError("source_changed", f"복사하는 동안 '{source_file}'가 다른 파일이나 링크로 바뀌었습니다.",
                       "파일을 바꾸는 작업(빌드·동기화·git checkout 등)이 끝난 뒤 다시 실행하세요.")


def _open_unchanged(source_file: Path, expected: os.stat_result) -> int:
    """링크를 따라가지 않고 열어, lstat 때와 같은 일반 파일(같은 장치·inode)인지 확인한 디스크립터를 돌려준다.

    왜: lstat로 일반 파일임을 본 뒤 open 전에 링크로 바뀌면, 그대로 열 경우 소스 밖 파일이 사본에 들어간다.
    주의: 바뀌었으면 source_changed. 그 밖의 OSError(권한 등)는 그대로 던진다.
    """
    try:
        descriptor = os.open(source_file, _READ_FLAGS)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _source_changed_error(source_file) from error
        raise
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise _source_changed_error(source_file)
    return descriptor


def _open_sink(destination_file: Path | None):
    """사본 파일을 새로(0600) 만들어 쓰기용으로 연다. destination_file이 없으면 아무것도 하지 않는 context를 준다."""
    if destination_file is None:
        return contextlib.nullcontext(None)
    return os.fdopen(os.open(destination_file, _WRITE_FLAGS, 0o600), "wb")


def _copy_file(source_file: Path, expected: os.stat_result, destination_file: Path | None,
               max_file_bytes: int | None) -> tuple[str, int]:
    """파일을 1 MiB씩 읽으며 sha256을 계산하고, destination_file이 있으면 함께 쓴다.

    expected: 순회 때의 lstat 결과. 연 파일이 이것과 다르면 source_changed(_open_unchanged 참고).
    사본 권한은 원본의 rwx 비트만 옮긴다(setuid·setgid·sticky는 버린다). 왜: 작업 사본은 빌드 재료일 뿐이라
    특수 권한 비트가 필요 없고, 남기면 사본의 실행 파일이 다른 권한으로 실행될 여지가 생긴다.
    반환: (hex digest, 바이트 수). 크기 상한은 읽는 도중에도 검사해 stat 이후 커진 파일도 막는다.
    """
    hasher, size = hashlib.sha256(), 0
    with os.fdopen(_open_unchanged(source_file, expected), "rb") as reader, _open_sink(destination_file) as sink:
        while chunk := reader.read(CHUNK_BYTES):
            size += len(chunk)
            if max_file_bytes is not None:
                _check_limit(f"파일 '{source_file.name}'의 크기", size, max_file_bytes, " 바이트")
            hasher.update(chunk)
            if sink:
                sink.write(chunk)
        if sink:
            os.fchmod(sink.fileno(), stat.S_IMODE(expected.st_mode) & _PERMISSION_BITS)
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
            if manifest.is_copy and _is_skipped_in_copy(entry, current == root, root, manifest):
                continue
            child_target = target / entry.name if target else None
            _visit(entry, child_target, root, manifest, source_root, stack)


def _is_skipped_in_copy(entry: os.DirEntry, is_root: bool, root: Path, manifest: _Manifest) -> bool:
    """복사할 때 빼는 항목인지 본다: 빌드·VCS 부산물 이름이거나 비밀 가능 파일(include 제외)."""
    return _is_excluded(entry.name, is_root) or _skips_credential(entry, root, manifest)


def _skips_credential(entry: os.DirEntry, root: Path, manifest: _Manifest) -> bool:
    """비밀 가능 이름의 파일·링크(디렉터리는 아님)이고 include에 없으면 기록·알림 후 True를 돌려준다(열지 않는다)."""
    if entry.is_dir(follow_symlinks=False) or not is_credential_name(entry.name):
        return False
    relative = Path(os.path.relpath(entry.path, root)).as_posix()
    if relative in manifest.include_paths:
        return False
    manifest.excluded.append(relative)
    manifest.notice(f"제외함(비밀 가능 파일): {relative}")
    return True


def _visit(entry: os.DirEntry, child_target: Path | None, root: Path, manifest: _Manifest,
           source_root: Path, stack: list) -> None:
    """항목 하나(디렉터리·링크·일반 파일·특수 파일)를 종류별로 처리한다."""
    path, relative = Path(entry.path), os.path.relpath(entry.path, root)
    info = os.lstat(path)
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        manifest.add(relative, "dir", 0, "")
        if child_target:
            child_target.mkdir()
            manifest.directory_modes.append((child_target, stat.S_IMODE(mode) & _PERMISSION_BITS))
        stack.append((path, child_target))
    elif stat.S_ISLNK(mode):
        _visit_link(path, relative, child_target, manifest, source_root)
    elif stat.S_ISREG(mode):
        _visit_regular_file(path, relative, info, child_target, manifest)
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


def _visit_regular_file(path: Path, relative: str, info: os.stat_result, child_target: Path | None,
                        manifest: _Manifest) -> None:
    """일반 파일의 크기 상한을 검사하고 복사·해시한 뒤 매니페스트에 기록한다. info: 순회 때의 lstat 결과."""
    max_file = manifest.limits.max_file_bytes if manifest.limits else None
    if max_file is not None:
        _check_limit(f"파일 '{relative}'의 크기", info.st_size, max_file, " 바이트")
    content_hash, size = _copy_file(path, info, child_target, max_file)
    manifest.add(relative, "file", info.st_mode & 0o111, content_hash)
    manifest.count_file(size)


def materialize_working_copy(source: Path, destination: Path, limits: CopyLimits = CopyLimits(), *,
                             include_paths: Collection[str] = frozenset(),
                             notice: Callable[[str], None] = print) -> WorkingCopy:
    """source를 destination으로 복사한다(빌드·VCS 부산물과 비밀 가능 파일 제외, 실행 권한 보존).

    include_paths: 비밀 가능 이름이어도 복사할 상대 POSIX 경로(reproof.toml [source] include, 검증된 값).
    notice: 제외한 비밀 가능 파일을 파일마다 한 번 '제외함(비밀 가능 파일): <경로>'로 알리는 함수.
    destination은 존재하면 안 된다(남은 이전 결과를 섞어 쓰지 않으려는 것). 실패하면 만들다 만
    destination을 지워 반쯤 복사된 사본이 재사용되지 않게 하고 오류는 그대로 다시 던진다.
    반환: WorkingCopy(excluded_paths는 정렬됨). 주의: 소스 밖을 가리키는 링크·특수 파일·상한 초과는 SimpleError.
    """
    source_root = Path(os.path.realpath(source))
    _validate_paths(source_root, destination)
    manifest = _Manifest(limits, is_copy=True)
    manifest.copy_root, manifest.include_paths, manifest.notice = destination, include_paths, notice
    destination.mkdir(parents=True)
    manifest.directory_modes.append((destination, stat.S_IMODE(os.stat(source_root).st_mode) & _PERMISSION_BITS))
    try:
        _walk(source_root, destination, source_root, manifest, source_root)
        _verify_links_inside_copy(manifest.links, destination)
        _apply_directory_modes(manifest.directory_modes)
    except BaseException as error:
        _discard_partial_copy(destination, error)
        raise
    return WorkingCopy(destination, manifest.digest(), manifest.file_count, manifest.total_bytes,
                       tuple(sorted(manifest.excluded)))


def _apply_directory_modes(directory_modes: list[tuple[Path, int]]) -> None:
    """디렉터리 권한을 가장 깊은 것부터 적용한다.

    왜: 읽기 전용(0o555) 소스 디렉터리를 만들 때 먼저 권한을 맞추면 그 안에 파일을 쓸 수 없다.
    자식을 먼저 처리해야 부모가 잠겨도 자식 chmod가 막히지 않는다.
    """
    for directory, mode in sorted(directory_modes, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(directory, mode)


def refuse_symlinked_tree(root: Path) -> None:
    """지우거나 권한을 고칠 트리의 루트가 심볼릭 링크면 workspace_symlink로 거절한다(링크 너머는 건드리지 않는다)."""
    if stat.S_ISLNK(os.lstat(root).st_mode):
        raise SimpleError("workspace_symlink",
                          f"지우려던 '{root}'가 심볼릭 링크입니다. 링크가 가리키는 폴더는 건드리지 않았습니다.",
                          "링크가 가리키는 곳을 확인한 뒤 링크만 직접 지우고(rm) 다시 실행하세요.")


def _make_tree_writable(root: Path) -> None:
    """삭제가 가능하도록 root 아래 모든 디렉터리에 u+rwx를 복구한다(링크는 따라가지 않는다).

    주의: root 자체가 링크면 workspace_symlink로 거절한다. 왜: 링크를 따라 chmod하면 작업 공간 밖 폴더 전체의
    권한이 바뀌고, 이어지는 삭제가 링크 너머를 지울 수 있기 때문이다.
    """
    refuse_symlinked_tree(root)
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
    except (OSError, SimpleError) as cleanup_error:
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


# ---------- 작업 공간 파일 쓰기: 기존 파일(inode)을 열어 자르지 않고, 새 파일을 만들어 바꿔 넣는다 ----------

def _create_private_temp(directory: Path) -> tuple[int, Path]:
    """directory 안에 새 0600 임시 파일을 O_EXCL로 만든다(tempfile.mkstemp). 반환: (읽기·쓰기 디스크립터, 경로).

    왜 같은 폴더인가: os.replace는 같은 파일 시스템 안에서만 원자적으로 이름을 바꿀 수 있다.
    """
    descriptor, name = tempfile.mkstemp(prefix=".reproof-", suffix=".tmp", dir=directory)
    return descriptor, Path(name)


def write_private_file(path: Path, data: bytes) -> None:
    """data를 새 0600 임시 파일에 쓴 뒤 os.replace로 path 자리에 바꿔 넣는다.

    왜: 기존 path를 열어 자르면(O_TRUNC) 하드 링크로 연결된 다른 파일까지 지워지고, 링크를 따라가면 다른 파일을 덮어쓴다.
    바꿔 넣기는 path의 이름만 새 파일로 옮기므로 연결된 파일은 그대로다. 실패하면 임시 파일을 지우고 오류를 다시 던진다.
    """
    descriptor, temp = _create_private_temp(path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def open_private_log(path: Path):
    """새 0600 파일을 만들어 path 자리로 바꿔 넣고, 그 파일에 이어 쓰는(O_APPEND) 바이너리 스트림을 돌려준다.

    왜: 세션 로그처럼 자식 프로세스가 계속 쓰는 파일도 기존 inode를 자르지 않고 새 파일로 시작하려는 것이다(write_private_file 참고).
    O_APPEND는 자식 프로세스와 reproof 진단 줄이 서로 덮어쓰지 않게 한다.
    """
    descriptor, temp = _create_private_temp(path.parent)
    try:
        os.replace(temp, path)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, fcntl.fcntl(descriptor, fcntl.F_GETFL) | os.O_APPEND)
    except BaseException:
        os.close(descriptor)
        temp.unlink(missing_ok=True)
        raise
    return os.fdopen(descriptor, "ab")


def _publish_temp(temp: Path, path: Path, in_flight: BaseException | None) -> None:
    """temp를 path 자리로 바꿔 넣는다. 실패하면 temp를 지우고, 진행 중인 예외가 있으면 그 예외에 사정을 덧붙인다.

    왜: 로그를 남기지 못한 문제가 원래 실패(빌드 오류·Ctrl-C)를 가리면 안 되고, 원래 실패가 없으면 그대로 알려야 한다.
    """
    try:
        os.replace(temp, path)
    except OSError as error:
        temp.unlink(missing_ok=True)
        if in_flight is None:
            raise
        in_flight.add_note(f"로그 {path}를 남기지 못했습니다: {error}")


@contextlib.contextmanager
def replacing_file(path: Path) -> Iterator[Path]:
    """path 옆에 새 0600 임시 파일을 만들어 그 경로를 주고, 끝나면(예외로 끝나도) path 자리로 바꿔 넣는다.

    왜: 외부 함수(repair.run_command)가 로그를 경로로만 받아 쓰므로, 그 함수가 기존 파일·링크를 열어 자르지 않도록
    항상 이번에 만든 새 파일 경로를 넘긴다. 폴더가 없으면 만든다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = _create_private_temp(path.parent)
    os.close(descriptor)
    try:
        yield temp
    except BaseException as error:
        _publish_temp(temp, path, error)
        raise
    _publish_temp(temp, path, None)
