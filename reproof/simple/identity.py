"""빌드 식별. 기록된 빌드를 개발자가 그대로 다시 만들 수 있도록 소스 버전과 설정 지문을 묶는다."""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolFailure
from .process import ToolRunner

# git 호출 제한 시간(초). 큰 저장소의 status도 이 안에 끝나야 한다.
_GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class BuildIdentity:
    """한 번의 녹화 빌드가 어떤 소스와 설정에서 나왔는지 나타낸다.

    commit_sha: HEAD 커밋(git이 아니거나 커밋이 없으면 None). is_dirty: 커밋 이후 변경 여부.
    dirty_patch_digest: 추적 파일 변경 patch의 sha256(없으면 None).
    untracked_paths: 추적되지 않는 파일 경로(정렬됨). config_digest/source_manifest_digest: 호출자가 계산한 지문.
    build_recipe: 툴의 빌드 방법(argv 규칙) 버전. xcode_version: `xcodebuild -version` 출력.
    왜 빌드 방법·Xcode까지 넣나: 소스와 설정이 같아도 빌드 방법(예: 서명 방식)이나 Xcode가 바뀌면 산출물이 달라지므로,
    같은 ID로 옛 산출물을 재사용하지 않게 하려는 것이다.
    """

    commit_sha: str | None
    is_dirty: bool
    dirty_patch_digest: str | None
    untracked_paths: tuple[str, ...]
    config_digest: str
    source_manifest_digest: str
    build_recipe: int
    xcode_version: str

    def to_document(self) -> dict:
        """JSON으로 저장할 수 있는 dict를 만든다. untracked_paths는 리스트로 바꾼다."""
        return {
            "commitSha": self.commit_sha,
            "isDirty": self.is_dirty,
            "dirtyPatchDigest": self.dirty_patch_digest,
            "untrackedPaths": list(self.untracked_paths),
            "configDigest": self.config_digest,
            "sourceManifestDigest": self.source_manifest_digest,
            "buildRecipe": self.build_recipe,
            "xcodeVersion": self.xcode_version,
        }

    def build_id(self) -> str:
        """정규화 JSON의 sha256 앞 16자를 돌려준다. 왜: 키 순서·공백과 무관하게 같은 입력이 같은 ID를 만들게 한다."""
        canonical = json.dumps(self.to_document(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def source_label(self) -> str:
        """사람이 읽는 소스 표시를 돌려준다: 'commit abc1234', 'commit abc1234 + 작업 중 변경', '소스 버전 미확인'."""
        if self.commit_sha is None:
            return "소스 버전 미확인"
        label = f"commit {self.commit_sha[:7]}"
        return f"{label} + 작업 중 변경" if self.is_dirty else label


def parse_porcelain_status(output: str) -> tuple[list[str], list[str]]:
    """`git status --porcelain=v1 -z` 출력을 (변경 경로, 추적 안 되는 경로)로 나눈다.

    NUL로 분리된 `XY path` 항목을 읽고, 이름 변경·복사(R/C)는 뒤따르는 원본 경로 항목을 건너뛴다.
    왜: 공백·한글·개행이 든 경로도 따옴표 처리 없이 정확히 얻기 위해서다.
    """
    entries = output.split("\0")
    changed: list[str] = []
    untracked: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            index += 1  # 원본 경로 항목
        (untracked if code == "??" else changed).append(path)
    return changed, untracked


def _run_git(runner: ToolRunner, source_root: Path, arguments: list[str]) -> str:
    """source_root에서 git을 실행해 stdout을 돌려준다. 실패하면 ToolFailure."""
    return runner.run(["git", "-C", str(source_root), *arguments], cwd=source_root, timeout=_GIT_TIMEOUT_SECONDS)


def _read_head_commit(runner: ToolRunner, source_root: Path) -> str | None:
    """source_root가 git 추적 내용일 때만 HEAD 커밋 SHA를 읽는다. 아니면 None.

    git 저장소가 아니거나, 커밋이 없거나, 상위 저장소 안에 있지만 추적 파일이 하나도 없는 폴더는
    '미확인'이다. 왜: 이런 소스도 빌드는 되어야 하고, 상위 저장소의 commit을 잘못 붙이면 안 된다.
    """
    try:
        sha = _run_git(runner, source_root, ["rev-parse", "--verify", "HEAD"]).strip()
        tracked = _run_git(runner, source_root, ["ls-files", "-z", "--", "."])
    except ToolFailure:
        return None
    return sha if sha and tracked else None


def _read_repository_prefix(runner: ToolRunner, source_root: Path) -> str:
    """저장소 루트에서 source_root까지의 상대 경로(끝에 '/', 루트면 빈 문자열)를 돌려준다."""
    return _run_git(runner, source_root, ["rev-parse", "--show-prefix"]).strip("\n")


def _relative_to_source_root(paths: list[str], prefix: str) -> list[str]:
    """저장소 루트 기준 경로에서 source_root 접두사를 떼어 source_root 기준으로 바꾼다."""
    return [path[len(prefix):] if path.startswith(prefix) else path for path in paths]


def _write_dirty_patch(runner: ToolRunner, source_root: Path, output_dir: Path) -> str | None:
    """추적 파일 변경을 dirty.patch로 쓰고 파일 바이트의 sha256을 돌려준다. 변경이 없으면 None.

    왜: 러너 stdout은 UTF-8로 복호화되며 손실되므로(EUC-KR 등) git이 파일에 직접 쓰게 한다.
    --relative와 '-- .'로 source_root 밖의 변경은 제외하고 경로를 source_root 기준으로 만든다.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    patch_path = output_dir / "dirty.patch"
    _run_git(runner, source_root, ["diff", "--binary", "--relative", f"--output={patch_path}", "HEAD", "--", "."])
    patch_bytes = patch_path.read_bytes()
    if not patch_bytes:
        patch_path.unlink()
        return None
    return hashlib.sha256(patch_bytes).hexdigest()


def capture_build_identity(source_root: Path, *, config_digest: str, source_manifest_digest: str,
                           output_dir: Path, runner: ToolRunner, build_recipe: int,
                           xcode_version: str) -> BuildIdentity:
    """source_root 범위의 git 상태를 읽어 BuildIdentity를 만든다. 추적 파일 변경이 있으면 dirty.patch를 쓴다.

    build_recipe·xcode_version은 호출자가 정한 빌드 방법 버전과 Xcode 버전으로, 그대로 식별에 넣는다.

    git 저장소가 아니거나 커밋이 없거나 추적 내용이 아니면 commit_sha=None, 변경 없음(오류 아님).
    상위 저장소의 source_root 밖 변경은 무시하고 경로는 source_root 기준이다. 삭제된 추적 파일도 변경이다.
    """
    commit_sha = _read_head_commit(runner, source_root)
    changed: list[str] = []
    untracked: list[str] = []
    patch_digest = None
    if commit_sha is not None:
        status = _run_git(runner, source_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."])
        changed, untracked = parse_porcelain_status(status)
        prefix = _read_repository_prefix(runner, source_root)
        untracked = _relative_to_source_root(untracked, prefix)
        patch_digest = _write_dirty_patch(runner, source_root, output_dir) if changed else None
    return BuildIdentity(commit_sha, bool(changed or untracked), patch_digest, tuple(sorted(untracked)),
                         config_digest, source_manifest_digest, build_recipe, xcode_version)
