"""비공개 SPM 검증용 테스트 도우미: Basic 인증을 요구하는 loopback git HTTP 서버와 임시 HOME 실행기."""
from __future__ import annotations

import base64
import hmac
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from reproof.simple.process import HostToolRunner


def _git_exec_path() -> Path:
    """`git --exec-path`로 git-http-backend가 있는 폴더를 찾는다. 왜: Xcode/Homebrew마다 위치가 달라서다."""
    output = subprocess.run(["git", "--exec-path"], check=True, capture_output=True, text=True).stdout
    return Path(output.strip())


class _GitHandler(BaseHTTPRequestHandler):
    """Basic 인증을 통과한 요청만 git http-backend(CGI)에 넘기는 핸들러."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 부모 시그니처
        """테스트 출력을 깨끗하게 두려고 접속 로그를 남기지 않는다."""

    def _is_authorized(self) -> bool:
        """Authorization 헤더가 기대한 username:token과 일치하는지 상수 시간 비교로 확인한다."""
        header = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(f"{self.server.username}:{self.server.token}".encode()).decode()
        return hmac.compare_digest(header, expected)

    def _reject(self) -> None:
        """401과 WWW-Authenticate를 보낸다. 요청 본문은 소비해 연결이 어긋나지 않게 한다."""
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="reproof-test"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cgi_environment(self) -> dict[str, str]:
        """git http-backend가 요구하는 CGI 환경 변수를 만든다. gzip 요청 본문은 HTTP_CONTENT_ENCODING으로 알린다."""
        parts = urlsplit(self.path)
        environment = {
            "PATH": os.environ.get("PATH", ""), "GIT_PROJECT_ROOT": str(self.server.root), "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parts.path, "REQUEST_METHOD": self.command, "QUERY_STRING": parts.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""), "REMOTE_USER": self.server.username,
            "REMOTE_ADDR": self.client_address[0], "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_PROTOCOL": "HTTP/1.1",
            "CONTENT_LENGTH": self.headers.get("Content-Length", ""),
        }
        if self.headers.get("Content-Encoding"):
            environment["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        return environment

    def _run_backend(self, body: bytes) -> bytes:
        """git http-backend를 실행해 CGI 응답 전체(헤더+본문)를 돌려준다."""
        backend = _git_exec_path() / "git-http-backend"
        result = subprocess.run([str(backend)], input=body, env=self._cgi_environment(), capture_output=True, check=False)
        return result.stdout

    def _send_cgi_response(self, output: bytes) -> None:
        """CGI 출력의 헤더(Status 포함)를 해석해 HTTP 응답으로 내보낸다."""
        head, _, payload = output.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode("iso-8859-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self) -> None:
        """인증을 확인하고 통과하면 backend에 위임한다."""
        if not self._is_authorized():
            return self._reject()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send_cgi_response(self._run_backend(body))

    do_GET = _handle
    do_POST = _handle


class AuthenticatedGitServer:
    """127.0.0.1 임의 포트에서 Basic 인증 뒤에 bare 저장소들을 제공하는 컨텍스트 매니저.

    repositories_root: `<name>.git` bare 저장소들이 있는 폴더. 왜 CGI인가: git http-backend가 smart HTTP를 정확히 구현해서다.
    """

    def __init__(self, repositories_root: Path, *, username: str, token: str):
        """루트와 자격 증명을 저장한다. 서버는 __enter__에서 시작한다."""
        self._root = repositories_root
        self._username = username
        self._token = token
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "AuthenticatedGitServer":
        """서버를 백그라운드 스레드에서 시작한다."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHandler)
        server.daemon_threads = True
        server.root, server.username, server.token = self._root, self._username, self._token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return self

    def __exit__(self, *exc_info) -> None:
        """서버를 멈추고 소켓을 닫는다."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def url_for(self, name: str) -> str:
        """저장소 이름(예: PrivateBadge.git)의 http URL을 돌려준다. 서버가 시작된 뒤에만 쓸 수 있다."""
        assert self._server is not None, "서버가 시작되지 않았습니다"
        return f"http://127.0.0.1:{self._server.server_address[1]}/{name}"


class HomeOverrideRunner(HostToolRunner):
    """사용자의 실제 HOME 대신 임시 HOME으로 명령을 실행하는 실행기.

    왜 GIT_CONFIG_NOSYSTEM/XDG_CONFIG_HOME인가: macOS git은 시스템 gitconfig의 osxkeychain helper와
    ~/.config/git 설정도 읽는다. 이를 막아야 실제 키체인이 섞이지 않아 테스트가 재현 가능하다.
    """

    def __init__(self, home: Path):
        """사용할 임시 HOME 경로를 저장한다."""
        self._home = home

    def run(self, argv, *, cwd, timeout, log_path=None, env_extra=None) -> str:
        """호출자의 env_extra에 HOME 등을 더해 부모 run을 부른다."""
        overrides = {"HOME": str(self._home), "GIT_CONFIG_NOSYSTEM": "1",
                     "XDG_CONFIG_HOME": str(self._home / ".config")}
        return super().run(argv, cwd=cwd, timeout=timeout, log_path=log_path, env_extra={**(env_extra or {}), **overrides})
