"""빌드 식별: 진짜 git 저장소로 commit·작업 중 변경·미확인 상태를 확인한다."""
import hashlib
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
    def capture(self, repo: Path, out: Path, config_digest: str = "c1", *, build_recipe: int = 2,
                xcode_version: str = "Xcode 27.0\nBuild version 27A266a") -> BuildIdentity:
        return capture_build_identity(repo, config_digest=config_digest, source_manifest_digest="m1",
                                      output_dir=out, runner=RUNNER, build_recipe=build_recipe,
                                      xcode_version=xcode_version)

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

    def test_build_id_depends_on_build_recipe_and_xcode_version(self):
        """빌드 방법 버전이나 Xcode가 바뀌면 다른 빌드 ID가 되어 이전 산출물을 재사용하지 않는다.

        왜: 서명 방식 같은 빌드 방법이 바뀌었는데 같은 ID로 옛 앱(App Group 없음)을 재사용하면 실행 직후 죽는다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repository(Path(tmp))
            base = self.capture(repo, Path(tmp) / "o1")
            new_recipe = self.capture(repo, Path(tmp) / "o2", build_recipe=3)
            new_xcode = self.capture(repo, Path(tmp) / "o3", xcode_version="Xcode 27.1\nBuild version 27B1")
            self.assertEqual(len({base.build_id(), new_recipe.build_id(), new_xcode.build_id()}), 3)
            document = base.to_document()
            self.assertEqual(document["buildRecipe"], 2)
            self.assertEqual(document["xcodeVersion"], "Xcode 27.0\nBuild version 27A266a")

    def make_nested(self, tmp: str) -> tuple[Path, Path]:
        """a.swift는 app/ 안, o는 other/ 안에 커밋된 저장소를 만들고 (루트, app)을 돌려준다."""
        repo = Path(tmp) / "repo"
        (repo / "app").mkdir(parents=True)
        (repo / "other").mkdir()
        git(repo, "init", "-q")
        (repo / "app" / "a.swift").write_text("a\n")
        (repo / "other" / "o").write_text("o\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        return repo, repo / "app"

    def test_changes_outside_subfolder_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, app = self.make_nested(tmp)
            (repo / "other" / "o").write_text("changed")
            (repo / "other" / "new").write_text("x")
            out = Path(tmp) / "out"
            identity = self.capture(app, out)
            self.assertFalse(identity.is_dirty)
            self.assertEqual(identity.untracked_paths, ())
            self.assertFalse((out / "dirty.patch").exists())

    def test_subfolder_changes_are_relative_to_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, app = self.make_nested(tmp)
            (app / "a.swift").write_text("changed\n")
            (app / "sub").mkdir()
            (app / "sub" / "u").write_text("x")
            (repo / "other" / "o").write_text("changed")
            out = Path(tmp) / "out"
            identity = self.capture(app, out)
            self.assertTrue(identity.is_dirty)
            self.assertEqual(identity.untracked_paths, ("sub/u",))
            patch = (out / "dirty.patch").read_text()
            self.assertIn("a/a.swift", patch)
            self.assertNotIn("other", patch)

    def test_untracked_folder_inside_parent_repository_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self.make_nested(tmp)
            plain = repo / "plain"
            plain.mkdir()
            (plain / "x.swift").write_text("x")
            identity = self.capture(plain, Path(tmp) / "out")
            self.assertIsNone(identity.commit_sha)
            self.assertEqual(identity.source_label(), "소스 버전 미확인")

    def test_non_utf8_change_is_preserved_in_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = make_repository(Path(tmp)), Path(tmp) / "out"
            (repo / "a.swift").write_bytes(b"caf\xe9\n")
            identity = self.capture(repo, out)
            patch_bytes = (out / "dirty.patch").read_bytes()
            self.assertIn(b"caf\xe9", patch_bytes)
            self.assertEqual(identity.dirty_patch_digest, hashlib.sha256(patch_bytes).hexdigest())

    def test_dirty_patch_never_contains_credential_files(self):
        """추적 중인 .env·키 파일의 변경은 dirty.patch에 넣지 않는다(작업 공간에 비밀이 남지 않게).

        변경 자체는 작업 중 변경으로 센다(is_dirty). 그런 파일만 바뀌었으면 patch 파일을 만들지 않는다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = make_repository(Path(tmp)), Path(tmp) / "out"
            (repo / "Config").mkdir()
            for name in (".env", "Config/AuthKey_X.p8"):
                (repo / name).write_text("A=1\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "secrets")
            for name in (".env", "Config/AuthKey_X.p8"):
                (repo / name).write_text("A=SECRET\n")
            only_secrets = self.capture(repo, Path(tmp) / "only")
            self.assertTrue(only_secrets.is_dirty)
            self.assertIsNone(only_secrets.dirty_patch_digest)
            self.assertFalse((Path(tmp) / "only" / "dirty.patch").exists())
            (repo / "a.swift").write_text("changed\n")
            self.capture(repo, out)
            patch = (out / "dirty.patch").read_text()
            self.assertIn("a.swift", patch)
            self.assertNotIn("SECRET", patch)

    def test_excluded_paths_are_part_of_identity(self):
        """작업 사본에서 뺀 파일 목록이 식별에 들어가 build_id를 바꾼다(사본에 무엇이 빠졌는지 기록)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repository(Path(tmp))
            base = self.capture(repo, Path(tmp) / "o1")
            excluded = capture_build_identity(repo, config_digest="c1", source_manifest_digest="m1",
                                              output_dir=Path(tmp) / "o2", runner=RUNNER, build_recipe=2,
                                              xcode_version="Xcode 27.0\nBuild version 27A266a",
                                              excluded_paths=(".env",))
            self.assertEqual(excluded.to_document()["excludedPaths"], [".env"])
            self.assertEqual(base.to_document()["excludedPaths"], [])
            self.assertNotEqual(base.build_id(), excluded.build_id())

    def test_rename_entry_skips_source_path(self):
        changed, untracked = parse_porcelain_status("R  new name.swift\0old.swift\0?? z.swift\0?? b.swift\0")
        self.assertEqual(changed, ["new name.swift"])
        self.assertEqual(untracked, ["z.swift", "b.swift"])


if __name__ == "__main__":
    unittest.main()
