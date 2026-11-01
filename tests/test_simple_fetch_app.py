"""검증 앱 받기 스크립트(scripts/simple-fetch-app.py) 계약. 로컬 bare 저장소를 pin 대상으로 쓰며 네트워크·Xcode가 필요 없다."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "simple-fetch-app.py"

GIT_ENVIRONMENT = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "reproof", "GIT_AUTHOR_EMAIL": "reproof@example.invalid",
                   "GIT_COMMITTER_NAME": "reproof", "GIT_COMMITTER_EMAIL": "reproof@example.invalid"}
"""테스트용 git 환경. 개발자의 전역·시스템 git 설정이 결과에 끼어들지 않게 막고 커밋 작성자를 고정한다."""


def git(*arguments: str, cwd: Path) -> str:
    """cwd에서 git을 실행하고 stdout을 돌려준다. 실패하면 CalledProcessError로 테스트를 바로 멈춘다."""
    return subprocess.run(["git", *arguments], cwd=cwd, env=GIT_ENVIRONMENT, check=True,
                          capture_output=True, text=True).stdout.strip()


class SimpleFetchAppTests(unittest.TestCase):
    """커밋 두 개와 annotated tag 하나가 있는 bare 저장소를 만들어 스크립트를 실제로 실행한다."""

    def setUp(self) -> None:
        """임시 폴더에 원본 저장소 → bare 저장소를 만들고 첫 커밋 SHA와 tag 객체 SHA를 기록한다.

        왜 첫 커밋을 pin으로 쓰나: 브랜치 끝(HEAD)이 아닌 커밋을 받아야 pin대로 checkout했다는 증거가 된다.
        """
        tmp = tempfile.TemporaryDirectory(prefix="reproof fetch 검증 ")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        work = self.tmp / "work"
        work.mkdir()
        git("init", "-q", "-b", "main", cwd=work)
        (work / "App.txt").write_text("첫 커밋\n", encoding="utf-8")
        git("add", "App.txt", cwd=work)
        git("commit", "-qm", "first", cwd=work)
        git("tag", "-a", "v1", "-m", "v1", cwd=work)
        (work / "App.txt").write_text("두 번째 커밋\n", encoding="utf-8")
        git("commit", "-qam", "second", cwd=work)
        bare = self.tmp / "app.git"
        git("clone", "-q", "--bare", str(work), str(bare), cwd=self.tmp)
        git("config", "uploadpack.allowFilter", "true", cwd=bare)  # 실제 GitHub처럼 blob:none 부분 clone을 받게 한다.
        self.repository = bare.as_uri()
        self.first_commit = git("rev-parse", "v1^{commit}", cwd=bare)
        self.tag_object = git("rev-parse", "v1", cwd=bare)
        self.output = self.tmp / "받은 앱" / "app"

    def run_fetch(self, commit: str) -> subprocess.CompletedProcess:
        """pin 파일을 쓰고 스크립트를 별도 프로세스로 실행해 결과(종료 코드·출력)를 돌려준다."""
        pin = self.tmp / "pin.json"
        pin.write_text(json.dumps({"repository": self.repository, "commit": commit}), encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), "--pin", str(pin), "--output", str(self.output)],
                              env=GIT_ENVIRONMENT, capture_output=True, text=True, timeout=120)

    def test_checks_out_pinned_commit_detached(self) -> None:
        """pin 커밋을 detached HEAD로 받고, 작업 트리가 그 커밋의 내용인지 확인한다."""
        result = self.run_fetch(self.first_commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.output), self.first_commit)
        self.assertEqual((self.output / "App.txt").read_text(encoding="utf-8"), "첫 커밋\n")
        detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self.output, env=GIT_ENVIRONMENT)
        self.assertNotEqual(detached.returncode, 0, "HEAD가 브랜치가 아니라 커밋을 직접 가리켜야 합니다.")
        self.assertIn(self.first_commit, result.stdout)

    def test_refuses_existing_output_directory(self) -> None:
        """출력 폴더가 이미 있으면 git을 실행하지 않고 거절하며, 안의 파일을 건드리지 않는다."""
        self.output.mkdir(parents=True)
        marker = self.output / "keep.txt"
        marker.write_text("기존 내용", encoding="utf-8")
        result = self.run_fetch(self.first_commit)
        self.assertEqual(result.returncode, 2)
        self.assertIn("문제:", result.stderr)
        self.assertIn(str(self.output), result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "기존 내용")
        self.assertFalse((self.output / ".git").exists())

    def test_refuses_head_that_differs_from_pin(self) -> None:
        """pin이 annotated tag 객체 SHA면 checkout 후 HEAD(커밋 SHA)와 달라 거절하고, 받은 폴더를 남기지 않는다.

        왜 tag 객체인가: `git ls-remote --tags`가 tag 객체 SHA를 먼저 보여 줘 실제로 잘못 복사하기 쉬운 값이다.
        """
        result = self.run_fetch(self.tag_object)
        self.assertEqual(result.returncode, 2)
        self.assertIn("문제:", result.stderr)
        self.assertIn(self.tag_object, result.stderr)
        self.assertIn(self.first_commit, result.stderr)
        self.assertFalse(self.output.exists(), "pin과 다른 소스가 받은 앱으로 남으면 안 됩니다.")


if __name__ == "__main__":
    unittest.main()
