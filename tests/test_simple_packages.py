"""프로젝트 생성기 실행과 SPM 해석, 인증 실패 안내 테스트. Xcode 없이 FakeToolRunner로 실행된다."""
import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reproof.simple.config import config_digest
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.generation import generate_project, generator_tool_path
from reproof.simple.packages import (classify_package_failure, find_package_resolved, redact_url,
                                     resolve_packages)
from tests.simple_support import FakeToolRunner, sample_config

RESOLVED_BYTES = b'{"pins": []}'


def _write_resolved(working_copy: Path, config) -> Path:
    """sample_config(project)의 Package.resolved 위치에 파일을 만든다."""
    path = working_copy / config.container / "project.xcworkspace" / "xcshareddata" / "swiftpm" / "Package.resolved"
    path.parent.mkdir(parents=True)
    path.write_bytes(RESOLVED_BYTES)
    return path


class SimplePackagesTests(unittest.TestCase):
    def _resolve(self, working_copy, config, runner=None, warnings=None):
        runner = runner or FakeToolRunner([(("xcodebuild",), "")])
        warnings = warnings if warnings is not None else []
        resolution = resolve_packages(working_copy, config, cache_root=working_copy / "cache", runner=runner,
                                      log_path=working_copy / "r.log", warn=warnings.append)
        return resolution, runner

    def test_resolve_uses_system_scm_cache_and_pinned_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config = Path(tmp) / "회사 앱", sample_config()
            _write_resolved(root, config)
            resolution, fake = self._resolve(root, config)
            argv = fake.calls[0]["argv"]
            self.assertEqual(argv[argv.index("-scmProvider") + 1], "system")
            self.assertIn("-onlyUsePackageVersionsFromResolvedFile", argv)
            self.assertIn("-project", argv)
            self.assertTrue(resolution.is_pinned)
            digest = hashlib.sha256(RESOLVED_BYTES).hexdigest()
            self.assertEqual(resolution.resolved_digest, digest)
            self.assertEqual(resolution.cache_dir, root / "cache" / "spm" / digest)
            self.assertTrue(resolution.cache_dir.is_dir())
            self.assertEqual(argv[argv.index("-clonedSourcePackagesDirPath") + 1], str(resolution.cache_dir))

    def test_resolve_without_resolved_file_warns_and_is_unpinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, warnings = Path(tmp), sample_config(), []
            resolution, fake = self._resolve(root, config, warnings=warnings)
            self.assertFalse(resolution.is_pinned)
            self.assertIsNone(resolution.resolved_digest)
            self.assertNotIn("-onlyUsePackageVersionsFromResolvedFile", fake.calls[0]["argv"])
            self.assertIn("Package.resolved", warnings[0])
            self.assertEqual(resolution.cache_dir.name, "unpinned-" + config_digest(config)[:16])

    def test_find_package_resolved_for_workspace_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = dataclasses.replace(sample_config(), container="App.xcworkspace")
            path = Path(tmp) / "App.xcworkspace" / "xcshareddata" / "swiftpm" / "Package.resolved"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"{}")
            self.assertEqual(find_package_resolved(Path(tmp), config), path)

    def test_auth_failure_is_classified_and_token_is_redacted(self):
        failure = ToolFailure("xcodebuild", log_path=None,
                              output_tail="fatal: Authentication failed for 'https://bot:s3cret@git.corp/x/Kit.git'")
        error = classify_package_failure(failure)
        self.assertEqual(error.code, "package_auth_failed")
        self.assertIn("https://git.corp/x/Kit.git", error.user_message())
        self.assertNotIn("s3cret", error.user_message())
        self.assertIn("git ls-remote", error.remedy)

    def test_auth_marker_found_in_log_file_and_other_failures_are_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_text("git@git.corp: Permission denied (publickey).\n", encoding="utf-8")
            error = classify_package_failure(ToolFailure("xcodebuild", log_path=log, output_tail=""))
            self.assertEqual(error.code, "package_auth_failed")
            self.assertIn("패키지 저장소", error.cause)
            other = classify_package_failure(ToolFailure("xcodebuild", log_path=log.with_name("none.log"),
                                                         output_tail="Package.swift: syntax error"))
            self.assertEqual(other.code, "package_resolution_failed")

    def test_resolve_converts_tool_failure_to_classified_error(self):
        failure = ToolFailure("xcodebuild", log_path=None, output_tail="terminal prompts disabled")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SimpleError) as caught:
            self._resolve(Path(tmp), sample_config(), FakeToolRunner([(("xcodebuild",), failure)]))
        self.assertEqual(caught.exception.code, "package_auth_failed")

    def test_redact_url_removes_userinfo_only(self):
        self.assertEqual(redact_url("https://u:p@h.com:8443/a.git"), "https://h.com:8443/a.git")
        self.assertEqual(redact_url("https://tok@h.com/a.git"), "https://h.com/a.git")
        self.assertEqual(redact_url("ssh://git@h.com/a.git"), "ssh://h.com/a.git")
        self.assertEqual(redact_url("git@github.com:org/a.git"), "git@github.com:org/a.git")
        self.assertEqual(redact_url("not a url"), "not a url")

    def test_missing_generator_tool_reports_install_command(self):
        with mock.patch("reproof.simple.generation.shutil.which", return_value=None):
            self.assertIsNone(generator_tool_path("xcodegen"))
            fake = FakeToolRunner([])
            with self.assertRaises(SimpleError) as caught:
                generate_project(Path("/x"), sample_config(), fake, log_path=Path("/x/g.log"))
            self.assertEqual(caught.exception.code, "generator_missing")
            self.assertIn("brew install xcodegen", caught.exception.remedy)
            tuist = dataclasses.replace(sample_config(), generator="tuist", generator_spec=None)
            with self.assertRaises(SimpleError) as tuist_caught:
                generate_project(Path("/x"), tuist, fake, log_path=Path("/x/g.log"))
            self.assertIn("mise install tuist", tuist_caught.exception.remedy)

    def test_xcodegen_runs_in_spec_directory(self):
        config = dataclasses.replace(sample_config(), generator_spec="앱 설정/project.yml")
        fake = FakeToolRunner([(("xcodegen",), "")])
        with mock.patch("reproof.simple.generation.shutil.which", return_value="/usr/local/bin/xcodegen"):
            generate_project(Path("/w/회사 앱"), config, fake, log_path=Path("/w/g.log"))
        self.assertEqual(fake.calls[0]["argv"], ["xcodegen", "generate", "--spec", "project.yml"])
        self.assertEqual(fake.calls[0]["cwd"], Path("/w/회사 앱/앱 설정"))

    def test_tuist_runs_in_root_and_none_does_nothing(self):
        fake = FakeToolRunner([(("tuist",), "")])
        tuist = dataclasses.replace(sample_config(), generator="tuist", generator_spec=None)
        with mock.patch("reproof.simple.generation.shutil.which", return_value="/usr/local/bin/tuist"):
            generate_project(Path("/w"), tuist, fake, log_path=Path("/w/g.log"))
        self.assertEqual(fake.calls[0]["argv"], ["tuist", "generate", "--no-open"])
        self.assertEqual(fake.calls[0]["cwd"], Path("/w"))
        none = dataclasses.replace(sample_config(), generator="none", generator_spec=None)
        generate_project(Path("/w"), none, fake, log_path=Path("/w/g.log"))
        self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
