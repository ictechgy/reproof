"""인증이 필요한 비공개 SPM 저장소를 실제 xcodebuild로 해석하는 native 테스트."""
from __future__ import annotations

import base64
import builtins
import dataclasses
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
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


def _git(*args: str, cwd: Path) -> None:
    """테스트용 git 명령을 실행한다. 실패하면 CalledProcessError로 바로 드러낸다(저장소 준비 오류를 숨기지 않기 위해).

    사용자·시스템 git 설정을 읽지 않는다(GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1). 왜: 개발자 Mac의 서명·hook·
    URL 재작성 설정이 테스트 저장소 준비에 섞이지 않게 하려는 것이다.
    """
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
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

    def _resolve(self, config: ProjectConfig = CONFIG):
        """임시 HOME 러너로 resolve_packages를 호출한다. config로 시간 제한 등을 바꿀 수 있다."""
        return resolve_packages(self.app, config, cache_root=self.scratch / "cache", runner=HomeOverrideRunner(self.home),
                                log_path=self.scratch / "resolve.log", warn=lambda _: None,
                                derived_data=self.scratch / "DerivedData")

    def test_private_package_resolves_with_existing_git_credentials(self) -> None:
        """기존 git 자격 증명(store helper)만으로 인증 필요한 패키지가 체크아웃된다."""
        self._write_credentials()
        resolution = self._resolve()
        checkouts = [p.name for p in (resolution.cache_dir / "checkouts").iterdir()]
        self.assertTrue(any(name.startswith("PrivateBadge") for name in checkouts), checkouts)
        self.assertTrue((self.home / "swiftpm-cache").is_dir(), "SwiftPM 캐시가 임시 HOME 밖에 만들어졌습니다")

    def test_missing_credentials_fail_fast_with_redacted_url(self) -> None:
        """자격 증명이 없으면 60초 안에 package_auth_failed로 실패하고 URL은 보이되 토큰은 없다."""
        started = time.monotonic()
        with self.assertRaises(SimpleError) as raised:
            self._resolve(dataclasses.replace(CONFIG, build_timeout_seconds=60))  # 60초 상한을 타임아웃으로 강제한다
        self.assertLess(time.monotonic() - started, 60)
        error = raised.exception
        self.assertEqual(error.code, "package_auth_failed", error.user_message())
        self.assertIn(self.package_url, error.user_message())
        self.assertNotIn(TOKEN, error.user_message())

    def _record_opened_paths(self, action) -> list[str]:
        """builtins.open, io.open, os.open, Path.open을 감싸 action 동안 파이썬이 연 경로를 모아 돌려준다.

        왜 여러 곳인가: Path.read_text/read_bytes와 직접 open, 저수준 os.open 모두 자격 증명 파일을 읽을 수 있어서다.
        """
        opened: list[str] = []
        originals = (builtins.open, io.open, os.open, pathlib.Path.open)

        def wrap(real):
            def spy(file, *args, **kwargs):
                opened.append(str(file))
                return real(file, *args, **kwargs)
            return spy

        def path_spy(path, *args, **kwargs):
            opened.append(str(path))
            return originals[3](path, *args, **kwargs)

        with mock.patch("builtins.open", wrap(originals[0])), mock.patch("io.open", wrap(originals[1])), \
                mock.patch("os.open", wrap(originals[2])), mock.patch.object(pathlib.Path, "open", path_spy):
            action()
        return opened

    def test_tool_never_reads_credentials_file(self) -> None:
        """툴(파이썬)이 임시 HOME 아래 어떤 경로도 열지 않는다. 자식 프로세스(git)는 읽어도 된다."""
        credentials = self._write_credentials()
        control = self._record_opened_paths(credentials.read_text)  # 양성 대조군: 스파이가 읽기를 잡아내야 한다
        self.assertIn(str(credentials), control)
        opened = self._record_opened_paths(self._resolve)
        self.assertTrue(opened, "스파이가 동작하지 않았습니다")
        under_home = [path for path in opened if str(self.home) in path]
        self.assertEqual(under_home, [])


class AuthenticatedGitServerTests(unittest.TestCase):
    """도우미 서버의 방어 동작(경로 탈출 403, 인증 401)을 확인한다."""

    def _status(self, server: AuthenticatedGitServer, path: str, *, authorized: bool) -> int:
        """서버에 GET 요청을 보내 HTTP 상태 코드를 돌려준다. authorized가 True면 올바른 Basic 인증을 붙인다."""
        request = urllib.request.Request(server.url_for(path))
        if authorized:
            request.add_header("Authorization", "Basic " + base64.b64encode(f"{USERNAME}:{TOKEN}".encode()).decode())
        try:
            return urllib.request.urlopen(request, timeout=10).status
        except urllib.error.HTTPError as error:
            return error.code

    def test_server_rejects_unauthenticated_and_path_traversal(self) -> None:
        """인증이 없으면 401, 인증이 있어도 `..` 경로는 403이다."""
        with tempfile.TemporaryDirectory() as root, AuthenticatedGitServer(Path(root), username=USERNAME, token=TOKEN) as server:
            self.assertEqual(self._status(server, "x.git/info/refs", authorized=False), 401)
            self.assertEqual(self._status(server, "../etc/passwd", authorized=True), 403)
            self.assertEqual(self._status(server, "%2e%2e/etc/passwd", authorized=True), 403)


if __name__ == "__main__":
    unittest.main()
