"""빌드 식별: 진짜 git 저장소로 commit·작업 중 변경·미확인 상태를 확인한다."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from reproof.simple.identity import BuildIdentity, capture_build_identity, parse_porcelain_status
from reproof.simple.process import HostToolRunner

RUNNER = HostToolRunner()


def git(repo: Path, *arguments: str) -> str:
    """테스트 저장소에서 git을 실행한다. 커밋 신원은 -c로 넘긴다(환경 변수가 걸러지므로)."""
    return RUNNER.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments], cwd=repo, timeout=60)


def make_repository(root: Path, *, commit: bool = True) -> Path:
    """a.swift 하나를 가진 저장소를 만든다."""
    repo = root / "회사 앱"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.swift").write_text("original\n")
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
    return repo


class SimpleIdentityTests(unittest.TestCase):
    def capture(self, repo: Path, out: Path, config_digest: str = "c1") -> BuildIdentity:
        return capture_build_identity(repo, config_digest=config_digest, source_manifest_digest="m1",
                                      output_dir=out, runner=RUNNER)

    def test_clean_repository_has_commit_and_not_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repository(Path(tmp))
            head = git(repo, "rev-parse", "HEAD").strip()
            identity = self.capture(repo, Path(tmp) / "out")
            self.assertEqual(identity.commit_sha, head)
            self.assertFalse(identity.is_dirty)
            self.assertEqual(identity.source_label(), f"commit {head[:7]}")
            self.assertFalse((Path(tmp) / "out" / "dirty.patch").exists())

    def test_modified_and_untracked_files_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = make_repository(Path(tmp)), Path(tmp) / "out"
            (repo / "a.swift").write_text("changed")
            (repo / "new.swift").write_text("x")
            identity = self.capture(repo, out)
            self.assertTrue(identity.is_dirty)
            self.assertEqual(identity.untracked_paths, ("new.swift",))
            self.assertTrue((out / "dirty.patch").read_text().startswith("diff --git"))
            self.assertIsNotNone(identity.dirty_patch_digest)
            self.assertTrue(identity.source_label().endswith("+ 작업 중 변경"))

    def test_only_untracked_files_is_dirty_without_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = make_repository(Path(tmp)), Path(tmp) / "out"
            (repo / "한글 파일.swift").write_text("x")
            identity = self.capture(repo, out)
            self.assertTrue(identity.is_dirty)
            self.assertIsNone(identity.dirty_patch_digest)
            self.assertEqual(identity.untracked_paths, ("한글 파일.swift",))
            self.assertFalse((out / "dirty.patch").exists())

    def test_deleted_tracked_file_is_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = make_repository(Path(tmp)), Path(tmp) / "out"
            (repo / "a.swift").unlink()
            identity = self.capture(repo, out)
            self.assertTrue(identity.is_dirty)
            self.assertIsNotNone(identity.dirty_patch_digest)

    def test_identity_for_repository_without_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repository(Path(tmp), commit=False)
            identity = self.capture(repo, Path(tmp) / "out")
            self.assertIsNone(identity.commit_sha)
            self.assertEqual(identity.source_label(), "소스 버전 미확인")

    def test_non_git_directory_is_unknown_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "plain"
            folder.mkdir()
            identity = self.capture(folder, Path(tmp) / "out")
            self.assertIsNone(identity.commit_sha)
            self.assertEqual(identity.source_label(), "소스 버전 미확인")

    def test_build_id_depends_on_config_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repository(Path(tmp))
            first = self.capture(repo, Path(tmp) / "o1", "c1")
            second = self.capture(repo, Path(tmp) / "o2", "c2")
            self.assertEqual(len(first.build_id()), 16)
            self.assertNotEqual(first.build_id(), second.build_id())
            self.assertEqual(first.build_id(), replace(first).build_id())

    def test_rename_entry_skips_source_path(self):
        changed, untracked = parse_porcelain_status("R  new name.swift\0old.swift\0?? z.swift\0?? b.swift\0")
        self.assertEqual(changed, ["new name.swift"])
        self.assertEqual(untracked, ["z.swift", "b.swift"])


if __name__ == "__main__":
    unittest.main()
