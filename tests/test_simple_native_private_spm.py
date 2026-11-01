"""인증이 필요한 비공개 SPM 저장소를 실제 xcodebuild로 해석하는 native 테스트."""
from __future__ import annotations

import builtins
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from reproof.simple.config import ProjectConfig, SimulatorSettings
from reproof.simple.errors import SimpleError
from reproof.simple.generation import generate_project
from reproof.simple.packages import resolve_packages
from reproof.simple.process import HostToolRunner
from tests.simple_git_server import AuthenticatedGitServer, HomeOverrideRunner

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "simple-ios"
USERNAME = "builder"
TOKEN = "s3cr3t-token-9f2a"
CONFIG = ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                       generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                       bundle_id="io.reproof.example.mixedshop", test_scheme="MixedShopTests",
                       simulator=SimulatorSettings("iPhone 16", "latest"))


# 커밋 시각 고정값. 왜: SwiftPM이 개발자 실제 ~/Library/org.swift.swiftpm(HOME과 무관하게 getpwuid 기준)에
# 패키지 지문(리비전)을 저장해, 실행마다 리비전이 달라지면 "does not match previously recorded value"로 실패한다.
# 같은 내용·시각이면 리비전이 항상 같아 반복 실행해도 지문이 일치한다.
_FIXED_DATE = "2026-01-01T00:00:00+0000"


def _git(*args: str, cwd: Path) -> None:
    """테스트용 git 명령을 실행한다. 실패하면 CalledProcessError로 바로 드러낸다(저장소 준비 오류를 숨기지 않기 위해)."""
    environment = {**os.environ, "GIT_AUTHOR_DATE": _FIXED_DATE, "GIT_COMMITTER_DATE": _FIXED_DATE}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True, env=environment)


def _publish_private_badge(repositories_root: Path, scratch: Path) -> None:
    """PrivateBadge 예제를 1.0.0 태그가 달린 bare 저장소(PrivateBadge.git)로 서버 루트에 만든다."""
    source = scratch / "PrivateBadge"
    shutil.copytree(EXAMPLES / "PrivateBadge", source)
    _git("init", "-q", "-b", "main", cwd=source)
    _git("add", ".", cwd=source)
    _git("commit", "-q", "-m", "init", cwd=source)
    _git("tag", "1.0.0", cwd=source)
    bare = repositories_root / "PrivateBadge.git"
    _git("clone", "-q", "--bare", str(source), str(bare), cwd=scratch)
    _git("update-server-info", cwd=bare)


def _prepare_app(scratch: Path, package_url: str) -> Path:
    """MixedShop 복사본에 비공개 패키지를 추가하고 XcodeGen으로 프로젝트를 만든다. 복사본 경로를 돌려준다."""
    app = scratch / "MixedShop"
    shutil.copytree(EXAMPLES / "MixedShop", app)
    spec = app / "project.yml"
    text = spec.read_text(encoding="utf-8")
    text = text.replace("packages:\n", f"packages:\n  PrivateBadge:\n    url: {package_url}\n    from: 1.0.0\n", 1)
    text = text.replace("    dependencies:\n      - package: ShopKit\n",
                        "    dependencies:\n      - package: ShopKit\n      - package: PrivateBadge\n", 1)
    spec.write_text(text, encoding="utf-8")
    generate_project(app, CONFIG, HostToolRunner(), log_path=scratch / "generate.log")
    return app


class SimpleNativePrivateSpmTests(unittest.TestCase):
    """loopback 인증 git 서버로 비공개 패키지 해석·인증 실패·자격 증명 비접근을 검증한다."""

    def setUp(self) -> None:
        """서버 루트·임시 HOME·작업 공간을 만들고 서버를 띄운다. 정리는 addCleanup으로 보장한다."""
        scratch_dir = tempfile.TemporaryDirectory()
        self.addCleanup(scratch_dir.cleanup)
        self.scratch = Path(scratch_dir.name)
        self.home = self.scratch / "home"
        self.home.mkdir()
        repositories = self.scratch / "repos"
        repositories.mkdir()
        _publish_private_badge(repositories, self.scratch)
        self.server = AuthenticatedGitServer(repositories, username=USERNAME, token=TOKEN)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.package_url = self.server.url_for("PrivateBadge.git")
        self.app = _prepare_app(self.scratch, self.package_url)

    def _write_credentials(self) -> Path:
        """임시 HOME에 credential.helper=store 설정과 서버 자격 증명 파일을 쓴다. 자격 증명 파일 경로를 돌려준다."""
        (self.home / ".gitconfig").write_text("[credential]\n\thelper = store\n", encoding="utf-8")
        credentials = self.home / ".git-credentials"
        host = self.package_url.split("://", 1)[1].split("/", 1)[0]
        credentials.write_text(f"http://{USERNAME}:{TOKEN}@{host}\n", encoding="utf-8")
        return credentials

    def _resolve(self):
        """임시 HOME 러너로 resolve_packages를 호출한다."""
        return resolve_packages(self.app, CONFIG, cache_root=self.scratch / "cache", runner=HomeOverrideRunner(self.home),
                                log_path=self.scratch / "resolve.log", warn=lambda _: None)

    def test_private_package_resolves_with_existing_git_credentials(self) -> None:
        """기존 git 자격 증명(store helper)만으로 인증 필요한 패키지가 체크아웃된다."""
        self._write_credentials()
        resolution = self._resolve()
        checkouts = [p.name for p in (resolution.cache_dir / "checkouts").iterdir()]
        self.assertTrue(any(name.startswith("PrivateBadge") for name in checkouts), checkouts)

    def test_missing_credentials_fail_fast_with_redacted_url(self) -> None:
        """자격 증명이 없으면 60초 안에 package_auth_failed로 실패하고 URL은 보이되 토큰은 없다."""
        started = time.monotonic()
        with self.assertRaises(SimpleError) as raised:
            self._resolve()
        self.assertLess(time.monotonic() - started, 60)
        error = raised.exception
        self.assertEqual(error.code, "package_auth_failed", error.user_message())
        self.assertIn(self.package_url, error.user_message())
        self.assertNotIn(TOKEN, error.user_message())

    def test_tool_never_reads_credentials_file(self) -> None:
        """툴(파이썬) 프로세스가 .git-credentials를 열지 않는다. 자식 프로세스(git)는 읽어도 된다."""
        credentials = self._write_credentials()
        opened: list[str] = []
        real_open, real_path_open = builtins.open, pathlib.Path.open

        def spy_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        def spy_path_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_path_open(path, *args, **kwargs)

        with mock.patch("builtins.open", spy_open), mock.patch.object(pathlib.Path, "open", spy_path_open):
            self._resolve()
        self.assertTrue(opened, "스파이가 동작하지 않았습니다")
        self.assertFalse([p for p in opened if p.endswith(".git-credentials")], credentials)


if __name__ == "__main__":
    unittest.main()
