"""같은 프로젝트·실행기를 두 실행이 동시에 다루지 못하게 하는 잠금 테스트. Xcode 없이 실제 flock으로 확인한다."""
import contextlib
import dataclasses
import fcntl
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reproof.simple.cli import main
from reproof.simple.config import CONFIG_FILE_NAME, render_config
from reproof.simple.errors import SimpleError
from reproof.simple.pipeline import prepare_build
from reproof.simple.runner_client import build_runner
from tests.simple_support import FakeToolRunner, sample_config

REPOSITORY = Path(__file__).resolve().parents[1]
# 다른 프로세스에서 프로젝트 잠금을 쥐고 "locked"를 알린 뒤 stdin이 닫힐 때까지 기다리는 스크립트.
HOLDER_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from reproof.simple.locking import project_lock\n"
    "with project_lock(Path(sys.argv[1]), sys.argv[2]):\n"
    "    print('locked', flush=True)\n"
    "    sys.stdin.read()\n"
)


class SimpleLockingTests(unittest.TestCase):
    """prepare_build·build_runner·inspect가 잠금이 걸려 있으면 아무것도 지우거나 바꾸지 않고 멈추는지 확인한다."""

    def setUp(self):
        """임시 작업 공간(공백·한글 포함)과 생성기 없는 설정을 준비한다."""
        directory = tempfile.TemporaryDirectory(prefix="잠금 ")
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.workspace = self.tmp / "작업 공간"
        self.source = self.tmp / "회사 앱"
        self.source.mkdir()
        (self.source / "a.swift").write_text("let a = 1\n", encoding="utf-8")
        self.config = dataclasses.replace(sample_config(), generator="none", generator_spec=None)
        self.project_dir = self.workspace / "projects" / self.config.name

    def hold_in_other_process(self):
        """다른 Python 프로세스가 프로젝트 잠금을 쥐게 하고, 잠금이 걸린 뒤 그 프로세스를 돌려준다. 끝나면 풀어 준다."""
        holder = subprocess.Popen([sys.executable, "-c", HOLDER_SCRIPT, str(self.workspace), self.config.name],
                                  cwd=REPOSITORY, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def hold_with_other_descriptor(self, path):
        """같은 프로세스에서 별도 파일 디스크립터로 path에 flock을 건다(다른 실행을 흉내 낸다)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def leftover_of_other_run(self):
        """다른 실행이 아직 쓰고 있는 staging·partial 폴더를 만들어 돌려준다(잠금이 지켜야 할 대상)."""
        staging = self.project_dir / "staging-0123.partial"
        partial = self.project_dir / "builds" / "abc.partial"
        for folder in (staging, partial):
            folder.mkdir(parents=True)
            (folder / "in-use.txt").write_text("사용 중", encoding="utf-8")
        return staging, partial

    def test_prepare_build_refuses_while_other_process_holds_project_lock(self):
        """다른 프로세스가 같은 프로젝트를 빌드 중이면 project_busy로 멈추고 그 실행의 폴더를 지우지 않는지 본다."""
        staging, partial = self.leftover_of_other_run()
        holder = self.hold_in_other_process()
        runner = FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.0\n")])
        with self.assertRaises(SimpleError) as raised:
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                          progress=lambda _: None)
        self.assertEqual(raised.exception.code, "project_busy")
        self.assertIn("다른 터미널", raised.exception.cause)
        self.assertTrue((staging / "in-use.txt").is_file())
        self.assertTrue((partial / "in-use.txt").is_file())
        self.assertEqual([call["argv"] for call in runner.calls], [["xcodebuild", "-version"]])  # 버전 확인만 했다.
        self.assertEqual(sorted(path.name for path in self.project_dir.iterdir()), [".lock", "builds", "staging-0123.partial"])
        holder.stdin.close()
        holder.wait(10)

    def test_lock_is_released_when_holder_ends(self):
        """잠금을 쥔 프로세스가 끝나면(정상 종료) 다음 실행이 같은 잠금을 얻는지 본다."""
        holder = self.hold_in_other_process()
        holder.stdin.close()
        holder.wait(10)
        from reproof.simple.locking import project_lock
        with project_lock(self.workspace, self.config.name):
            pass

    def test_build_runner_refuses_while_runner_lock_is_held(self):
        """실행기 잠금이 다른 디스크립터에 걸려 있으면 runner-src를 지우지 않고 runner_busy로 멈추는지 본다."""
        output = self.workspace / "runner"
        (output / "runner-src").mkdir(parents=True)
        (output / "runner-src" / "keep.txt").write_text("빌드 중", encoding="utf-8")
        self.hold_with_other_descriptor(output / ".lock")
        with self.assertRaises(SimpleError) as raised:
            build_runner(output, FakeToolRunner([]))
        self.assertEqual(raised.exception.code, "runner_busy")
        self.assertTrue((output / "runner-src" / "keep.txt").is_file())

    def test_inspect_refuses_before_touching_simulator_while_project_is_busy(self):
        """다른 실행이 프로젝트 잠금을 쥐고 있으면 inspect가 빌드·Simulator 초기화 전에 project_busy로 멈추는지 본다."""
        config_path = self.source / CONFIG_FILE_NAME
        config_path.write_text(render_config(self.config), encoding="utf-8")
        self.hold_with_other_descriptor(self.project_dir / ".lock")
        runner = FakeToolRunner([])
        err = io.StringIO()
        with mock.patch("reproof.simple.cli._new_runner", return_value=runner), \
                mock.patch.dict(os.environ, {"REPROOF_APP_API_TOKEN": "t"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["inspect", "--config", str(config_path), "--workspace", str(self.workspace)])
        self.assertEqual(code, 2)
        self.assertIn("다른 터미널에서 같은 프로젝트", err.getvalue())
        self.assertEqual(runner.calls, [])

    def test_simulator_lock_is_global_per_user_and_private(self):
        """Simulator 잠금은 작업 공간과 무관한 `<임시 폴더>/reproof-<uid>/sim-<기기>.lock`이고 폴더는 0700이다.

        왜: 작업 공간이 다른 두 실행이 같은 `reproof-*` 기기를 동시에 초기화하지 못하게 하려는 것이다.
        """
        from reproof.simple.locking import simulator_lock
        with mock.patch("reproof.simple.locking.tempfile.gettempdir", return_value=str(self.tmp)):
            directory = self.tmp / f"reproof-{os.getuid()}"
            self.hold_with_other_descriptor(directory / "sim-reproof-shop-abc123.lock")
            with self.assertRaises(SimpleError) as raised, simulator_lock("reproof-shop-abc123"):
                pass
            self.assertEqual(raised.exception.code, "simulator_busy")
            self.assertIn("reproof-shop-abc123", raised.exception.cause)
            with simulator_lock("reproof-other-def456"):
                self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_simulator_lock_refuses_directory_owned_by_someone_else_or_link(self):
        """잠금 폴더가 링크이거나 다른 사용자 것이면 쓰지 않고 lock_directory_untrusted로 멈춘다(공유 /tmp 대비)."""
        from reproof.simple.locking import simulator_lock
        with mock.patch("reproof.simple.locking.tempfile.gettempdir", return_value=str(self.tmp)):
            (self.tmp / "elsewhere").mkdir()
            os.symlink(self.tmp / "elsewhere", self.tmp / f"reproof-{os.getuid()}")
            with self.assertRaises(SimpleError) as raised, simulator_lock("reproof-shop-abc123"):
                pass
            self.assertEqual(raised.exception.code, "lock_directory_untrusted")
            self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_inspect_refuses_before_touching_simulator_while_device_is_busy(self):
        """다른 작업 공간의 실행이 같은 기기의 Simulator 잠금을 쥐고 있으면 simctl을 부르기 전에 simulator_busy로 멈춘다."""
        from reproof.simple.simulator import owned_device_name
        config_path = self.source / CONFIG_FILE_NAME
        config_path.write_text(render_config(self.config), encoding="utf-8")
        runner, err = FakeToolRunner([]), io.StringIO()
        device_name = owned_device_name(self.config.name, self.source)
        with mock.patch("reproof.simple.locking.tempfile.gettempdir", return_value=str(self.tmp)):
            self.hold_with_other_descriptor(self.tmp / f"reproof-{os.getuid()}" / f"sim-{device_name}.lock")
            with mock.patch("reproof.simple.cli._new_runner", return_value=runner), \
                    mock.patch("reproof.simple.cli.prepare_build", return_value=mock.Mock()), \
                    mock.patch.dict(os.environ, {"REPROOF_APP_API_TOKEN": "t"}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = main(["inspect", "--config", str(config_path), "--workspace", str(self.workspace)])
        self.assertEqual(code, 2)
        self.assertIn(device_name, err.getvalue())
        self.assertEqual(runner.calls, [])

    def test_lock_is_reentrant_within_one_process(self):
        """inspect처럼 이미 잠금을 쥔 프로세스가 prepare_build를 불러도 스스로 막히지 않는지 본다."""
        from reproof.simple.locking import project_lock
        runner = FakeToolRunner([(("xcodebuild", "-version"), "Xcode 27.0\n"), (("git",), "")])
        with project_lock(self.workspace, self.config.name), self.assertRaises(AssertionError):
            # 잠금은 통과하고 빌드 단계에서 대역에 없는 호출로 멈춘다(잠금 때문에 멈추지 않았다는 뜻).
            prepare_build(self.source, self.config, workspace_root=self.workspace, runner=runner,
                          progress=lambda _: None)


if __name__ == "__main__":
    unittest.main()
