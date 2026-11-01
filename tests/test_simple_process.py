"""간편 모드의 오류 형식, 명령 실행기, CLI 진입점 계약. Xcode 없이 실행된다."""
import contextlib
import errno
import io
import os
import random
import string
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import reproof.cli
from reproof import repair
from reproof.repair import run_command
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.process import HostToolRunner, summarize_tool_output
from tests.simple_support import FakeToolRunner, spawn_zombie_group


def _fake_secret(prefix: str, length: int, alphabet: str, seed: int) -> str:
    """테스트용 가짜 비밀 값을 실행 시점에 만든다.

    왜 이렇게 하는가: 실제 토큰처럼 보이는 문자열을 공개 저장소에 그대로 적으면 비밀 정보 스캐너가 오탐하고
    진짜 토큰과 구별하기 어렵다. 가리기 규칙은 반복 문자열을 무작위로 보지 않으므로, 고정 seed의 의사 난수로
    형식(접두사·길이·문자 종류)과 무작위성만 같은 값을 만든다. 같은 seed라 실행마다 값이 같다.
    """
    return prefix + "".join(random.Random(seed).choices(alphabet, k=length))


_ALNUM = string.ascii_letters + string.digits
FAKE_GITHUB_TOKEN = _fake_secret("ghp_", 36, _ALNUM, 20261101)
FAKE_URLSAFE_SECRET = _fake_secret("", 43, _ALNUM + "-_", 20261102)


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

    def run_failing(self, script, *, timeout=10, argv=None):
        """로그 경로 없이 실패하는 명령을 HostToolRunner로 실행해 ToolFailure를 돌려준다.

        임시 폴더를 시스템 임시 위치로 바꿔, 끝난 뒤 임시 로그가 남지 않았는지도 확인한다.
        """
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tempfile, "tempdir", tmp):
            with self.assertRaises(ToolFailure) as caught:
                HostToolRunner().run(argv or ["/bin/sh", "-c", script], cwd=Path(tmp), timeout=timeout)
            self.assertEqual(os.listdir(tmp), [])
        self.assertIsNone(caught.exception.log_path)
        return caught.exception

    def test_failure_without_log_path_shows_last_output_lines_and_exit_code(self):
        """log_path 없이 실패해도 임시 로그로 출력을 받아 원인에 마지막 줄과 종료 코드를 싣고, 임시 로그는 지우는지 본다.

        왜: simctl·git·init의 xcodebuild 조회는 log_path가 없어, 예전에는 "실행이 실패했습니다"만 보여 원인을 알 수 없었다.
        """
        failure = self.run_failing("echo 진행 중; echo 'error: 기기를 찾을 수 없음' >&2; exit 3")
        self.assertIn("error: 기기를 찾을 수 없음", failure.cause)
        self.assertIn("종료 코드 3", failure.cause)
        self.assertIn("error: 기기를 찾을 수 없음", failure.output_tail)
        self.assertNotIn("로그를 확인", failure.remedy)

    def test_failure_cause_removes_control_characters_and_redacts_url_userinfo(self):
        """출력의 ANSI escape·제어 문자를 지우고 URL의 사용자 정보(토큰)를 가린 뒤 원인에 싣는지 본다."""
        failure = self.run_failing(r"printf '\033[31mfatal: https://bot:s3cret@git.corp/x/Kit.git\a\n' >&2; exit 1")
        self.assertIn("https://git.corp/x/Kit.git", failure.cause)
        self.assertNotIn("s3cret", failure.cause)
        self.assertFalse(any(ord(character) < 32 or ord(character) == 127 for character in failure.cause),
                         repr(failure.cause))

    def test_failure_cause_output_is_limited_to_300_characters(self):
        """아주 긴 출력도 원인에는 300자까지만 싣는지 본다(안내문 도배 방지)."""
        failure = self.run_failing("printf 'word %.0s' $(seq 1 1000) >&2; exit 1")
        summary = failure.cause.split("마지막 출력: ", 1)[1]
        self.assertIn("word word", summary)
        self.assertLessEqual(len(summary), 300)

    def test_timeout_is_reported_with_limit_and_distinct_remedy(self):
        """시간 초과는 종료 코드 실패와 구분해 "시간 제한 N초"로 알리고, timeout_seconds에 제한 값을 남기는지 본다."""
        failure = self.run_failing("sleep 5", timeout=1)
        self.assertIn("시간 제한 1초를 넘겼습니다", failure.cause)
        self.assertEqual(failure.timeout_seconds, 1)
        self.assertNotIn("종료 코드", failure.cause)

    def test_missing_executable_is_reported_as_not_started(self):
        """실행 파일이 없으면 시작하지 못했다고 알리는지 본다."""
        failure = self.run_failing("", argv=["/없는/도구/simctl"])
        self.assertIn("simctl", failure.cause)
        self.assertIn("시작하지 못했습니다", failure.cause)

    def test_tool_log_replaces_link_without_touching_linked_file(self):
        """도구 로그 자리에 하드·심볼릭 링크가 있어도 연결된 다른 파일을 자르거나 덮어쓰지 않고 새 0600 파일로 바꿔 넣는다.

        왜: 로그 파일을 열어 자르면(O_TRUNC·write_bytes) 하드 링크로 연결된 사용자 파일까지 내용이 사라진다.
        """
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.txt"
            victim.write_text("건드리면 안 됨", encoding="utf-8")
            for name, make in (("hard.log", os.link), ("soft.log", os.symlink)):
                with self.subTest(link=name):
                    log = Path(tmp) / name
                    make(victim, log)
                    HostToolRunner().run(["/bin/echo", "출력"], cwd=Path(tmp), timeout=30, log_path=log)
                    self.assertEqual(victim.read_text(encoding="utf-8"), "건드리면 안 됨")
                    self.assertFalse(log.is_symlink())
                    self.assertEqual((os.stat(log).st_nlink, os.stat(log).st_mode & 0o777), (1, 0o600))
                    self.assertIn("출력", log.read_text(encoding="utf-8"))
            self.assertEqual(sorted(path.name for path in Path(tmp).iterdir()), ["hard.log", "soft.log", "victim.txt"])

    def test_host_runner_passes_git_terminal_prompt_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = HostToolRunner().run(["/bin/sh", "-c", "echo $GIT_TERMINAL_PROMPT"], cwd=Path(tmp), timeout=10)
        self.assertEqual(output.strip(), "0")

    def test_proxy_and_git_transport_variables_are_passed_through(self):
        """회사 네트워크용 프록시·CA·git SSH 변수가 있으면 값을 바꾸지 않고 도구에 넘기고, 없으면 넣지 않는지 본다.

        왜: run_command는 HOME·PATH 등 최소 환경만 물려줘, 프록시 뒤의 Mac에서 비공개 SPM·git이 실패했다.
        """
        names = ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                 "GIT_SSH_COMMAND", "SSL_CERT_FILE", "GIT_SSL_CAINFO")
        values = {name: f"값-{index}" for index, name in enumerate(names)}
        script = "; ".join(f'echo "{name}=${{{name}-unset}}"' for name in names)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, values):
                output = HostToolRunner().run(["/bin/sh", "-c", script], cwd=Path(tmp), timeout=10)
            self.assertEqual(output.splitlines(), [f"{name}={values[name]}" for name in names])
            with mock.patch.dict(os.environ, {}, clear=False):
                for name in names:
                    os.environ.pop(name, None)
                output = HostToolRunner().run(["/bin/sh", "-c", script], cwd=Path(tmp), timeout=10)
            self.assertEqual(output.splitlines(), [f"{name}=unset" for name in names])

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



class ToolOutputMaskingTests(unittest.TestCase):
    """안내문에 싣는 마지막 출력(summary)에서 자격 증명으로 보이는 값을 가리는지 확인한다(보안 검토 지시)."""

    def assertMasked(self, line, secret, kept=()):
        """line을 요약하면 secret이 사라지고 ***가 생기며, kept의 글자는 그대로 남는지 확인한다."""
        summary = summarize_tool_output(line)
        self.assertNotIn(secret, summary)
        self.assertIn("***", summary)
        for text in kept:
            self.assertIn(text, summary)

    def test_credential_query_values_are_masked(self):
        """token·key·sig·password·secret·auth·X-Amz-* 같은 이름의 쿼리 값은 가리고 다른 값은 둔다."""
        self.assertMasked("fetch https://cdn.example.com/a.zip?X-Amz-Signature=abcd1234&X-Amz-Credential=AKIAXY%2F1&page=2",
                          "abcd1234", kept=("X-Amz-Signature=***", "page=2", "cdn.example.com/a.zip"))
        self.assertMasked("GET /api?Token=s3cr3t&lang=ko", "s3cr3t", kept=("Token=***", "lang=ko"))
        for name in ("access_token", "key", "apikey", "api_key", "sig", "signature", "password", "passwd",
                     "secret", "auth"):
            with self.subTest(name=name):
                self.assertMasked(f"url https://h.example/p?{name}=hunter2", "hunter2", kept=(f"{name}=***",))

    def test_authorization_header_and_auth_scheme_values_are_masked(self):
        """Authorization 헤더 값 전체와 Bearer·Basic 뒤의 값을 가린다."""
        self.assertMasked("> Authorization: token abc123", "abc123", kept=("Authorization: ***",))
        self.assertMasked("header Bearer eyJhbGciOiJIUzI1NiJ9.payload", "eyJhbGciOiJIUzI1NiJ9", kept=("Bearer ***",))
        self.assertMasked("sent Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA", kept=("Basic ***",))

    def test_long_high_entropy_runs_are_masked(self):
        """32자 이상 무작위 토큰(GitHub 토큰, base64 비밀, 16진 키, '/'가 섞인 키)을 가린다."""
        for token in (FAKE_GITHUB_TOKEN, FAKE_URLSAFE_SECRET,
                      "7276a711f33fc88eff7217122b731c57a19ebff671a94dcc0c09d9eb9d86e26f",
                      "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
            with self.subTest(token=token):
                self.assertMasked(f"fatal: bad credential {token} rejected", token, kept=("fatal: bad credential",))

    def test_ordinary_paths_error_words_and_numbers_are_not_masked(self):
        """일반 경로·DerivedData 폴더 이름·UUID·오류 문장·짧은 숫자는 그대로 둔다."""
        lines = ["/Users/me/Library/Developer/Xcode/DerivedData/MixedShop-gbhzqfzkcyrbqvdjxzwkxvqzglky/Build/Products",
                 "Sources/Feature2/LoginViewController.swift:12:5: error: cannot find 'Kit' in scope",
                 "Invalid device: DC1C6A87-3B2F-4E5A-9C1D-0123456789AB",
                 "error: authorization failed; basic configuration missing (exit code 65)",
                 "~/.reproof/projects/my-app/builds/7276a711f33fc88eff7217122b731c57a19ebff671a94dcc0c09d9eb9d86e26f/logs"]
        for line in lines[:4]:
            with self.subTest(line=line):
                self.assertEqual(summarize_tool_output(line), line)
        self.assertEqual(summarize_tool_output(lines[4]), "~/.reproof/projects/my-app/builds/***/logs")

    def test_jwt_is_masked_as_one_token(self):
        """점 세 조각 JWT(eyJ…)는 조각이 짧아도 통째로 가린다(쿼리 값이 아니어도)."""
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        for line in (f"token rejected: {jwt}", f"https://h.example/cb?state=1#id_token={jwt}"):
            with self.subTest(line=line):
                summary = summarize_tool_output(line)
                for piece in jwt.split("."):
                    self.assertNotIn(piece, summary)

    def test_credential_query_keys_match_by_suffix_and_substring(self):
        """refresh_token·id_token·client_secret·access-token·api-key처럼 접미·부분 일치하는 쿼리 이름의 값을 가린다."""
        for name in ("refresh_token", "id_token", "client_secret", "access-token", "api-key", "x-api-key",
                     "db_password", "user_passwd", "aws_secret_key", "pagetoken"):
            with self.subTest(name=name):
                self.assertMasked(f"url https://h.example/p?mode=1&{name}=hunter2&lang=ko", "hunter2",
                                  kept=(f"{name}=***", "mode=1", "lang=ko"))
        self.assertEqual(summarize_tool_output("GET /p?page=2&monkey=banana"), "GET /p?page=2&monkey=banana")

    def test_every_query_key_is_examined_even_after_nested_separators(self):
        """일반 인자의 값이 뒤따르는 `?`·`;`를 삼켜 다음 `token=`이 검사되지 않는 일이 없어야 한다(가리기 우회 방지)."""
        for line, kept in (("GET https://h.example/p?a=x;token=hunter2", ("a=x", "token=***")),
                           ("GET https://h.example/p?a=x?access_token=hunter2&lang=ko", ("a=x", "access_token=***",
                                                                                        "lang=ko")),
                           ("GET /p;mode=dev;client_secret=hunter2;v=1", ("mode=dev", "client_secret=***", "v=1"))):
            with self.subTest(line=line):
                self.assertMasked(line, "hunter2", kept=kept)

    def test_base64_secret_with_slashes_is_judged_as_a_whole(self):
        """'/'가 섞인 base64 비밀은 낱말 같은 조각(info, abc)이 끼어 있어도 조각마다가 아니라 전체로 판단해 가린다."""
        secret = "info/Zx9Kq2Lm8Pw3Ds/abc/Rt5Yh7Uj1Ok4Nb6VcX"
        summary = summarize_tool_output(f"fatal: bad key {secret} rejected")
        self.assertNotIn("Zx9Kq2Lm8Pw3Ds", summary)
        self.assertNotIn("Rt5Yh7Uj1Ok4Nb6VcX", summary)
        plus = "ab/cdEfgh9Kq2+Lm8Pw3Ds/xyzRt5Yh7Uj1Ok4N=="
        self.assertNotIn("Lm8Pw3Ds", summarize_tool_output(f"secret {plus} rejected"))

    def test_log_tail_drops_partial_first_line_before_masking(self):
        """로그 끝 4096바이트 창이 줄 중간(예: 'https:' 바로 뒤)에서 시작하면 그 조각 줄을 버린다.

        왜: '//bot:pw@host'처럼 스킴이 잘린 조각은 redact_url이 알아보지 못해, 뒤가 빈 줄뿐이면 비밀번호가 안내문에 남는다.
        """
        from reproof.simple.process import read_log_tail
        fragment = "//bot:hunter2pw@git.corp/x.git"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_text("x" * 100 + "https:" + fragment + "\n" * (4096 - len(fragment)), encoding="utf-8")
            self.assertNotIn("hunter2pw", summarize_tool_output(read_log_tail(log)))
            short = Path(tmp) / "short.log"
            short.write_text("first line\nlast line\n", encoding="utf-8")
            self.assertEqual(read_log_tail(short), "first line\nlast line\n")

    def test_overlong_line_drops_partial_first_token_before_masking(self):
        """4096자를 넘는 줄은 끝쪽만 보되, 창 시작에서 잘린 첫 조각(쿼리 이름이 잘린 비밀 등)은 버리고 가린다."""
        from reproof.simple.redaction import mask_secrets
        line = "fetch ?access_token=" + "SECRETVALUEabc" + " " + "w " * 2041
        cut = len(line) - 4096
        self.assertTrue(20 < cut < 34, cut)  # 창이 비밀 값 가운데에서 시작한다.
        self.assertNotIn(line[cut:34], mask_secrets(line))

    def test_adversarial_long_input_is_processed_quickly(self):
        """긴 적대적 입력도 1초 안에 처리하는지 본다(ReDoS 회귀 방지, 보안 검토 지시).

        왜 이 입력들인가: 소문자 연속 뒤에 실패하는 꼬리(숫자)가 오는 조각은 예전 낱말 정규식에서 지수 시간
        역추적을 일으켰다. 첫 조각에 두어 낱말 검사가 반드시 그 조각부터 보게 한다. 하나라도 1초를 넘으면 바로 멈춘다.
        """
        for line in ("a" * 50000 + "!", "a" * 27 + "1/src", "a" * 250 + "1/src", "a" * 50000 + "1/src"):
            started = time.perf_counter()
            summarize_tool_output(line)
            self.assertLess(time.perf_counter() - started, 1.0, f"{len(line)}자 입력: {line[-8:]!r}")

    def test_host_runner_summary_masks_secrets_end_to_end(self):
        """실제 실행 실패의 안내문에서도 토큰이 가려지는지 본다(URL 사용자 정보 가리기와 함께)."""
        script = ("echo 'curl -H \"Authorization: Bearer " + FAKE_GITHUB_TOKEN + "\" "
                  "https://bot:pw@git.corp/x.git?token=abc' >&2; exit 1")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ToolFailure) as caught:
            HostToolRunner().run(["/bin/sh", "-c", script], cwd=Path(tmp), timeout=10)
        message = caught.exception.user_message()
        for secret in (FAKE_GITHUB_TOKEN, "bot:pw", "token=abc"):
            self.assertNotIn(secret, message)


class RunCommandGroupCleanupTests(unittest.TestCase):
    """repair.run_command가 끝난 뒤 프로세스 그룹을 정리할 때 macOS의 일시적 EPERM을 견디는지 확인한다."""

    def test_successful_command_survives_zombie_only_group_until_reaped(self):
        """좀비만 남은 그룹(macOS는 실제 커널 EPERM)이 100 ms 뒤 회수되면 성공한 명령이 그대로 성공으로 끝나는지 본다.

        Linux는 좀비 그룹에도 신호가 성공해 EPERM 재시도 없이 끝나는 경로를 확인한다(건너뛰지 않는다).

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

    def test_leader_is_reaped_even_when_group_signals_keep_failing(self):
        """시간 초과 정리에서 그룹 신호가 계속 EPERM이어도 리더를 회수(wait)한 뒤 PermissionError를 던지는지 본다.

        왜: 예전에는 SIGTERM의 PermissionError가 SIGKILL과 wait()를 건너뛰어, 끝나지 않은 자식이 회수되지 않은 채 남았다.
        """
        denied = PermissionError(errno.EPERM, "Operation not permitted")
        spawned, real_popen = [], repair.subprocess.Popen

        def recording_popen(*arguments, **options):
            spawned.append(real_popen(*arguments, **options))
            return spawned[-1]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(repair, "_GROUP_SIGNAL_RETRY_SECONDS", 0.05, create=True), \
                mock.patch.object(repair.subprocess, "Popen", side_effect=recording_popen), \
                mock.patch("os.killpg", side_effect=denied), self.assertRaises(PermissionError):
            run_command(["/bin/sleep", "1"], tmp, timeout=0.2)
        self.assertIsNotNone(spawned[0].returncode)

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
