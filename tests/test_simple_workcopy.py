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

    def test_large_file_copied_in_chunks(self):
        source = self.tmp / "src"
        (source).mkdir()
        (source / "big.bin").write_bytes(b"ab" * (1024 * 1024 + 7))
        copy = materialize_working_copy(source, self.tmp / "dest")
        self.assertEqual((self.tmp / "dest/big.bin").read_bytes(), b"ab" * (1024 * 1024 + 7))
        self.assertEqual(copy.manifest_digest, tree_digest(self.tmp / "dest"))


if __name__ == "__main__":
    unittest.main()
