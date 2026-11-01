"""간편 모드의 오류 형식, 명령 실행기, CLI 진입점 계약. Xcode 없이 실행된다."""
import contextlib
import errno
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import reproof.cli
from reproof import repair
from reproof.repair import run_command
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.process import HostToolRunner
from tests.simple_support import FakeToolRunner, spawn_zombie_group


class SimpleProcessTests(unittest.TestCase):
    def test_user_message_contains_cause_remedy_and_log(self):
        error = SimpleError("x", "빌드가 실패했습니다.", "로그를 확인하세요.", log_path=Path("/tmp/a.log"))
        self.assertEqual(error.user_message(), "문제: 빌드가 실패했습니다.\n해결: 로그를 확인하세요.\n로그: /tmp/a.log")

    def test_user_message_omits_log_line_without_path(self):
        error = SimpleError("x", "원인", "해결")
        self.assertEqual(error.user_message(), "문제: 원인\n해결: 해결")

    def test_host_runner_wraps_command_error_as_tool_failure(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ToolFailure) as caught:
            HostToolRunner().run(["/bin/sh", "-c", "echo boom >&2; exit 3"], cwd=Path(tmp), timeout=10,
                                 log_path=Path(tmp) / "x.log")
        self.assertEqual(caught.exception.code, "tool_failed")
        self.assertIn("boom", caught.exception.output_tail)

    def test_tool_failure_without_log_has_empty_tail(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ToolFailure) as caught:
            HostToolRunner().run(["/bin/sh", "-c", "exit 3"], cwd=Path(tmp), timeout=10)
        self.assertEqual(caught.exception.output_tail, "")

    def test_host_runner_passes_git_terminal_prompt_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = HostToolRunner().run(["/bin/sh", "-c", "echo $GIT_TERMINAL_PROMPT"], cwd=Path(tmp), timeout=10)
        self.assertEqual(output.strip(), "0")

    def test_caller_env_extra_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = HostToolRunner().run(["/bin/sh", "-c", "echo $GIT_TERMINAL_PROMPT"], cwd=Path(tmp), timeout=10,
                                          env_extra={"GIT_TERMINAL_PROMPT": "1"})
        self.assertEqual(output.strip(), "1")

    def test_fake_runner_records_calls_and_rejects_unknown(self):
        fake = FakeToolRunner([(("xcodebuild", "-list"), "ok")])
        self.assertEqual(fake.run(["xcodebuild", "-list", "-json"], cwd=Path("."), timeout=1), "ok")
        self.assertEqual(fake.calls[0]["argv"], ["xcodebuild", "-list", "-json"])
        with self.assertRaises(AssertionError):
            fake.run(["git", "status"], cwd=Path("."), timeout=1)

    def test_app_cli_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as code:
            reproof.cli.main(["app", "--help"])
        self.assertEqual(code.exception.code, 0)
        self.assertIn("init", out.getvalue())

    def test_app_cli_simple_error_returns_two_with_message(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = reproof.cli.main(["app", "build", "--config", "/없는/경로/reproof.toml"])
        self.assertEqual(code, 2)
        self.assertIn("문제:", err.getvalue())



class RunCommandGroupCleanupTests(unittest.TestCase):
    """repair.run_command가 끝난 뒤 프로세스 그룹을 정리할 때 macOS의 일시적 EPERM을 견디는지 확인한다."""

    def test_successful_command_survives_zombie_only_group_until_reaped(self):
        """좀비만 남은 그룹(실제 커널 EPERM)이 100 ms 뒤 회수되면 성공한 명령이 그대로 성공으로 끝나는지 본다.

        왜: 예전에는 ProcessLookupError만 잡아, 성공한 명령도 PermissionError 트레이스백으로 실패했다.
        killpg를 좀비 그룹으로 돌려 보내 실제 EPERM을 받게 하고, 첫 killpg 호출 100 ms 뒤 다른 스레드가 좀비를 회수한다.
        """
        zombie = spawn_zombie_group()
        real_killpg = os.killpg
        reaper = threading.Timer(0.1, zombie.wait)

        def killpg_into_zombie_group(pid, number):
            """첫 호출에서 회수 타이머를 켜고, 신호는 좀비 그룹으로 보낸다."""
            if not reaper.is_alive() and not reaper.finished.is_set():
                reaper.start()
            return real_killpg(zombie.pid, number)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("os.killpg", side_effect=killpg_into_zombie_group):
            try:
                output = run_command(["/bin/sh", "-c", "echo ok"], tmp, timeout=10)
            finally:
                if reaper.is_alive():
                    reaper.join()
                zombie.wait()
        self.assertTrue(reaper.finished.is_set())
        self.assertEqual(output, "ok\n")

    def test_group_that_stays_eperm_is_retried_then_reraised(self):
        """재시도 한도 뒤에도 EPERM이면 예전처럼 PermissionError를 다시 던지는지(실패 쪽으로 닫힘) 본다."""
        denied = PermissionError(errno.EPERM, "Operation not permitted")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(repair, "_GROUP_SIGNAL_RETRY_SECONDS", 0.2, create=True), \
                mock.patch("os.killpg", side_effect=denied) as killpg, self.assertRaises(PermissionError):
            run_command(["/bin/sh", "-c", "exit 0"], tmp, timeout=10)
        self.assertGreater(killpg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
