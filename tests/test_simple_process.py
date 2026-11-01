"""간편 모드의 오류 형식, 명령 실행기, CLI 진입점 계약. Xcode 없이 실행된다."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import reproof.cli
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.process import HostToolRunner
from tests.simple_support import FakeToolRunner


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

    def test_app_cli_not_ready_returns_two_with_message(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = reproof.cli.main(["app", "doctor"])
        self.assertEqual(code, 2)
        self.assertIn("문제:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
