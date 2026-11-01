"""앱 소스 작업 사본 계약. Xcode 없이 실행된다."""
import os
import tempfile
import unittest
from pathlib import Path

from reproof.simple.errors import SimpleError
from reproof.simple.workcopy import CopyLimits, materialize_working_copy, tree_digest


def write(path: Path, text: str = "x", mode: int | None = None) -> None:
    """부모 디렉터리를 만들며 파일을 쓴다. mode가 있으면 권한을 지정한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


class WorkingCopyTests(unittest.TestCase):
    def setUp(self):
        """테스트마다 임시 루트를 만든다."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_copies_resources_and_preserves_executable_bit(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift")
        write(source / "Assets.xcassets/Contents.json", "{}")
        write(source / "scripts/run.sh", "#!/bin/sh\n", 0o755)
        copy = materialize_working_copy(source, dest)
        self.assertTrue((dest / "Assets.xcassets/Contents.json").exists())
        self.assertTrue(os.access(dest / "scripts/run.sh", os.X_OK))
        self.assertEqual(copy.file_count, 3)
        self.assertEqual(copy.total_bytes, 1 + 2 + 10)

    def test_excludes_git_deriveddata_xcuserdata_and_root_build(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        for name in (".git/x", "DerivedData/x", "App.xcodeproj/xcuserdata/x", "build/x", ".reproof/x", "Sources/.build/x"):
            write(source / name)
        write(source / "Sources/build/keep.swift")
        materialize_working_copy(source, dest)
        for name in (".git", "DerivedData", "build", ".reproof", "App.xcodeproj/xcuserdata", "Sources/.build"):
            self.assertFalse((dest / name).exists(), name)
        self.assertTrue((dest / "Sources/build/keep.swift").exists())

    def test_internal_relative_symlink_is_recreated_and_external_symlink_rejected(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift")
        os.symlink("a.swift", source / "link.swift")
        materialize_working_copy(source, dest)
        self.assertTrue((dest / "link.swift").is_symlink())
        self.assertEqual(os.readlink(dest / "link.swift"), "a.swift")
        source2, dest2 = self.tmp / "src2", self.tmp / "dest2"
        write(source2 / "a.swift")
        os.symlink("/etc/hosts", source2 / "bad")
        with self.assertRaises(SimpleError) as caught:
            materialize_working_copy(source2, dest2)
        self.assertEqual(caught.exception.code, "source_symlink_outside")
        self.assertFalse(dest2.exists())

    def test_relative_symlink_through_absolute_path_points_inside_copy(self):
        """'../'를 충분히 써서 원본 절대 경로로 돌아가는 상대 링크도 사본 안의 같은 파일을 가리켜야 한다.

        왜: 원문 그대로 다시 만들면 사본의 링크가 원본 저장소를 가리켜, 생성기가 그 링크로 원본을 고칠 수 있다.
        """
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift", "original")
        (source / "deep" / "er").mkdir(parents=True)
        absolute = os.path.realpath(source / "a.swift").lstrip("/")
        os.symlink("../" * 40 + absolute, source / "deep" / "er" / "link.swift")
        os.symlink(os.path.realpath(source / "deep"), source / "abs-dir")
        copy = materialize_working_copy(source, dest)
        real_dest = os.path.realpath(dest)
        for link, expected in (("deep/er/link.swift", "a.swift"), ("abs-dir", "deep")):
            with self.subTest(link=link):
                self.assertEqual(os.path.realpath(dest / link), os.path.join(real_dest, expected))
        self.assertEqual(os.readlink(dest / "deep" / "er" / "link.swift"), "../../a.swift")
        self.assertEqual(copy.manifest_digest, tree_digest(dest))

    def test_chained_relative_links_inside_source_are_kept_verbatim(self):
        """소스 안에 머무는 상대 링크는 바이트 그대로 둔다(링크를 거친 링크도 펼치지 않는다).

        왜: 서명된 framework·XCFramework는 `Headers -> Versions/Current/Headers` 같은 링크 문자열까지 봉인한다.
        끝까지 푼 `Versions/A/Headers`로 바꾸면 `codesign --verify --strict`가 "sealed resource is missing or invalid"로
        실패한다.
        """
        source, dest = self.tmp / "src", self.tmp / "dest"
        framework = source / "Vendor" / "Kit.framework"
        write(framework / "Versions" / "A" / "Headers" / "Kit.h", "// kit")
        write(framework / "Versions" / "A" / "Kit", "binary")
        expected = {"Versions/Current": "A", "Headers": "Versions/Current/Headers", "Kit": "Versions/Current/Kit"}
        for name, text in expected.items():
            os.symlink(text, framework / name)
        os.symlink("./Kit.framework/../Kit.framework/Headers/Kit.h", source / "Vendor" / "dotted.h")
        materialize_working_copy(source, dest)
        copied = dest / "Vendor" / "Kit.framework"
        for name in ("Headers", "Kit", "Versions/Current"):
            with self.subTest(link=name):
                self.assertEqual(os.readlink(copied / name), expected[name])
        self.assertEqual(os.readlink(dest / "Vendor" / "dotted.h"), "./Kit.framework/../Kit.framework/Headers/Kit.h")
        self.assertEqual((copied / "Headers" / "Kit.h").read_text(), "// kit")

    def test_relative_link_that_leaves_source_lexically_is_rewritten_inside_copy(self):
        """상대 링크가 글자상 소스 밖으로 나갔다 들어오면(`../src/a.swift`) 원문을 쓰지 않고 사본 안 경로로 다시 만든다."""
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift", "original")
        os.symlink("../src/a.swift", source / "back.swift")
        materialize_working_copy(source, dest)
        self.assertEqual(os.readlink(dest / "back.swift"), "a.swift")
        self.assertEqual(os.path.realpath(dest / "back.swift"), os.path.join(os.path.realpath(dest), "a.swift"))

    def test_file_swapped_for_symlink_after_lstat_is_not_followed(self):
        """lstat 뒤 open 전에 파일이 밖을 가리키는 링크로 바뀌어도 그 대상을 읽지 않고 source_changed로 멈춘다."""
        from unittest import mock
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift", "original")
        write(self.tmp / "outside" / "secret.txt", "SECRET")
        target, real_lstat, swapped = source / "a.swift", os.lstat, []
        real_target = Path(os.path.realpath(target))

        def swapping_lstat(path, *arguments, **options):
            result = real_lstat(path, *arguments, **options)
            if Path(path) == real_target and not swapped:
                swapped.append(True)
                os.unlink(target)
                os.symlink(self.tmp / "outside" / "secret.txt", target)
            return result

        with mock.patch("reproof.simple.workcopy.os.lstat", side_effect=swapping_lstat):
            with self.assertRaises(SimpleError) as caught:
                materialize_working_copy(source, dest)
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertFalse(dest.exists())

    def test_make_tree_writable_refuses_symlink_root(self):
        """정리 대상 루트가 링크면 링크 너머 폴더의 권한을 바꾸지 않고 거절한다."""
        from reproof.simple.workcopy import _make_tree_writable
        victim = self.tmp / "victim" / "locked"
        victim.mkdir(parents=True)
        victim.chmod(0o500)
        self._make_removable_on_cleanup(self.tmp / "victim")
        os.symlink(self.tmp / "victim", self.tmp / "link")
        with self.assertRaises(SimpleError) as caught:
            _make_tree_writable(self.tmp / "link")
        self.assertEqual(caught.exception.code, "workspace_symlink")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o500)

    def test_setuid_setgid_and_sticky_bits_are_not_copied(self):
        """복사한 파일·폴더에는 setuid·setgid·sticky 비트를 남기지 않는다(실행 비트는 그대로)."""
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "tool", "#!/bin/sh\n", 0o4755)
        (source / "shared").mkdir()
        (source / "shared").chmod(0o1777)
        materialize_working_copy(source, dest)
        self.assertEqual((dest / "tool").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((dest / "shared").stat().st_mode & 0o7777, 0o777)

    CREDENTIAL_PATHS = (".env", "config/.env.production", ".git-credentials", ".netrc", ".npmrc",
                        "fastlane/AuthKey_AB12.p8", "certs/dev.p12", "certs/dev.pfx", "certs/server.pem",
                        "certs/server.key", "android/release.keystore", "android/release.jks",
                        "profiles/App.mobileprovision", "profiles/App.provisionprofile", "keys/id_rsa",
                        "keys/id_ed25519.pub", "certs/apple.cer", "certs/UPPER.P12")
    """비밀이 들어 있을 수 있는 파일 이름 예(대문자 확장자 포함)."""

    def test_credential_files_are_excluded_without_opening_and_reported_once(self):
        """비밀 가능 파일은 열지도 않고 빼며, 파일마다 한 번 '제외함(비밀 가능 파일): <경로>'를 알린다.

        왜: 작업 사본은 ~/.reproof에 남고 나중에 AI가 읽을 수 있으므로 .env·서명 키가 들어가면 안 된다.
        권한 0을 줘 열면 PermissionError가 나게 해 '열지 않음'을 확인한다.
        """
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "Keychain.swift")
        write(source / "environment.swift")
        for name in self.CREDENTIAL_PATHS:
            write(source / name, "SECRET", 0o000)
        os.symlink("Keychain.swift", source / "certs" / "linked.pem")
        notices = []
        copy = materialize_working_copy(source, dest, notice=notices.append)
        expected = tuple(sorted((*self.CREDENTIAL_PATHS, "certs/linked.pem")))
        for name in expected:
            self.assertFalse(os.path.lexists(dest / name), name)
        self.assertEqual(copy.excluded_paths, expected)
        self.assertEqual(sorted(notices), [f"제외함(비밀 가능 파일): {name}" for name in expected])
        self.assertTrue((dest / "Keychain.swift").exists())
        self.assertTrue((dest / "environment.swift").exists())
        self.assertEqual(copy.manifest_digest, tree_digest(dest))

    def test_included_credential_file_is_copied(self):
        """[source] include로 지정한 상대 경로의 파일은 비밀 가능 이름이어도 복사한다."""
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "Config" / ".env.dev", "MODE=dev")
        write(source / ".env", "SECRET")
        copy = materialize_working_copy(source, dest, include_paths=frozenset({"Config/.env.dev"}),
                                        notice=lambda _: None)
        self.assertEqual((dest / "Config" / ".env.dev").read_text(), "MODE=dev")
        self.assertEqual(copy.excluded_paths, (".env",))

    def test_file_count_limit_reports_limit_and_remedy(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        for name in "abc":
            write(source / f"{name}.swift")
        with self.assertRaises(SimpleError) as caught:
            materialize_working_copy(source, dest, CopyLimits(max_files=2))
        self.assertEqual(caught.exception.code, "source_too_large")
        self.assertIn("2", caught.exception.cause)
        self.assertIn("3", caught.exception.cause)
        self.assertTrue(caught.exception.remedy)
        self.assertFalse(dest.exists())

    def test_single_file_and_total_size_limits(self):
        source = self.tmp / "src"
        write(source / "big.bin", "x" * 10)
        for limits in (CopyLimits(max_file_bytes=5), CopyLimits(max_total_bytes=5)):
            with self.assertRaises(SimpleError) as caught:
                materialize_working_copy(source, self.tmp / "d", limits)
            self.assertEqual(caught.exception.code, "source_too_large")

    def test_handles_space_and_korean_path(self):
        source = self.tmp / "회사 앱" / "My App"
        write(source / "화면 1.swift", "내용")
        dest = self.tmp / "작업 사본"
        copy = materialize_working_copy(source, dest)
        self.assertEqual(copy.manifest_digest, tree_digest(dest))
        self.assertTrue((dest / "화면 1.swift").exists())

    def test_destination_must_not_exist(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift")
        dest.mkdir()
        with self.assertRaises(SimpleError) as caught:
            materialize_working_copy(source, dest)
        self.assertEqual(caught.exception.code, "destination_exists")

    def test_special_file_rejected(self):
        source = self.tmp / "src"
        write(source / "a.swift")
        os.mkfifo(source / "pipe")
        with self.assertRaises(SimpleError) as caught:
            materialize_working_copy(source, self.tmp / "dest")
        self.assertEqual(caught.exception.code, "source_special_file")

    def test_digest_is_stable_and_sensitive_to_content_and_exec_bit(self):
        source = self.tmp / "src"
        write(source / "a.sh", "1", 0o644)
        first = materialize_working_copy(source, self.tmp / "d1").manifest_digest
        self.assertEqual(first, materialize_working_copy(source, self.tmp / "d2").manifest_digest)
        (source / "a.sh").chmod(0o755)
        self.assertNotEqual(first, materialize_working_copy(source, self.tmp / "d3").manifest_digest)
        write(source / "a.sh", "2", 0o644)
        self.assertNotEqual(first, materialize_working_copy(source, self.tmp / "d4").manifest_digest)

    def _make_removable_on_cleanup(self, *roots: Path) -> None:
        """테스트가 만든 읽기 전용 디렉터리를 정리 전에 쓰기 가능으로 되돌린다."""
        def restore():
            for root in roots:
                for directory, _, _ in os.walk(root):
                    os.chmod(directory, 0o755)
        self.addCleanup(restore)

    def test_read_only_source_directory_is_copied_and_mode_preserved(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "ro/a", "1")
        write(source / "ro/inner/b", "2")
        (source / "ro/inner").chmod(0o555)
        (source / "ro").chmod(0o555)
        self._make_removable_on_cleanup(source, dest)
        copy = materialize_working_copy(source, dest)
        self.assertEqual(copy.file_count, 2)
        self.assertEqual((dest / "ro").stat().st_mode & 0o777, 0o555)
        self.assertEqual((dest / "ro/inner").stat().st_mode & 0o777, 0o555)
        self.assertEqual(copy.manifest_digest, tree_digest(dest))

    def test_failed_copy_removes_partial_destination_even_with_read_only_dirs(self):
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "ro/a", "1")
        (source / "ro").chmod(0o555)
        write(source / "z/bad", "1")
        os.symlink("/etc/hosts", source / "z/link")
        self._make_removable_on_cleanup(source)
        with self.assertRaises(SimpleError) as caught:
            materialize_working_copy(source, dest)
        self.assertEqual(caught.exception.code, "source_symlink_outside")
        self.assertFalse(dest.exists())

    def test_cleanup_failure_is_reported_with_leftover_path(self):
        from unittest import mock
        source, dest = self.tmp / "src", self.tmp / "dest"
        write(source / "a.swift")
        os.symlink("/etc/hosts", source / "bad")
        with mock.patch("reproof.simple.workcopy.shutil.rmtree", side_effect=OSError("busy")):
            with self.assertRaises(SimpleError) as caught:
                materialize_working_copy(source, dest)
        self.assertEqual(caught.exception.code, "source_symlink_outside")
        self.assertIn(str(dest), caught.exception.remedy)
        self.assertIn("busy", caught.exception.remedy)

    def test_large_file_copied_in_chunks(self):
        source = self.tmp / "src"
        (source).mkdir()
        (source / "big.bin").write_bytes(b"ab" * (1024 * 1024 + 7))
        copy = materialize_working_copy(source, self.tmp / "dest")
        self.assertEqual((self.tmp / "dest/big.bin").read_bytes(), b"ab" * (1024 * 1024 + 7))
        self.assertEqual(copy.manifest_digest, tree_digest(self.tmp / "dest"))


if __name__ == "__main__":
    unittest.main()
