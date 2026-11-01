"""비공개 SPM 검증용 테스트 도우미: Basic 인증을 요구하는 loopback git HTTP 서버와 임시 HOME 실행기."""
from __future__ import annotations

import base64
import hmac
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

    def _is_path_allowed(self) -> bool:
        """PATH_INFO에 `..`이 없고 저장소 루트 아래로 풀리는지 확인한다. 왜: 루트 밖 파일 노출을 막기 위해서다."""
        path = unquote(urlsplit(self.path).path)
        if ".." in path:
            return False
        root = self.server.root.resolve()
        return (root / path.lstrip("/")).resolve().is_relative_to(root)

    def _send_error_text(self, status: int, message: str) -> None:
        """상태 코드와 짧은 텍스트 본문으로 오류 응답을 보낸다."""
        payload = message.encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _run_backend(self, body: bytes) -> subprocess.CompletedProcess:
        """git http-backend를 실행해 종료 코드·CGI 출력(헤더+본문)·stderr를 돌려준다."""
        backend = _git_exec_path() / "git-http-backend"
        return subprocess.run([str(backend)], input=body, env=self._cgi_environment(), capture_output=True, check=False)

    def _send_cgi_response(self, output: bytes) -> None:
        """CGI 출력의 헤더(Status 포함)를 해석해 HTTP 응답으로 내보낸다."""
        head, _, payload = output.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode("iso-8859-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                try:
                    status = int(value.strip().split()[0])
                except (ValueError, IndexError):
                    return self._send_error_text(500, f"잘못된 CGI Status 헤더: {line!r}")
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
        if not self._is_path_allowed():
            return self._send_error_text(403, "허용되지 않은 경로입니다.")
        result = self._run_backend(body)
        if result.returncode != 0:
            return self._send_error_text(500, "git http-backend 실패: " + result.stderr.decode("utf-8", "replace"))
        self._send_cgi_response(result.stdout)

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
        """호출자의 env_extra에 HOME 등을 더해 부모 run을 부른다. xcodebuild에는 -packageCachePath를 덧붙인다.

        왜: xcodebuild는 SwiftPM 저장소 캐시를 $HOME과 무관하게 실제 ~/Library에 두므로, 테스트가 사용자 캐시를
        건드리지 않게 임시 HOME 안으로 돌린다. 제품 코드(packages.py)는 일부러 사용자의 기본 캐시를 그대로 쓴다.
        한계(실측): 이 플래그로도 manifest 캐시(~/Library/Caches/org.swift.swiftpm/manifests)는 실제 위치에 갱신되고,
        패키지 지문(~/Library/org.swift.swiftpm/security)은 여전히 읽힌다. 실행마다 리비전이 달라지면 지문 불일치로
        실패하므로 -packageFingerprintPolicy warn으로 경고만 하게 한다(실측: 지문 파일은 더 이상 쓰이지 않음).
        """
        if Path(argv[0]).name == "xcodebuild":
            argv = [*argv, "-packageCachePath", str(self._home / "swiftpm-cache"),
                    "-packageFingerprintPolicy", "warn"]
        overrides = {"HOME": str(self._home), "GIT_CONFIG_NOSYSTEM": "1",
                     "XDG_CONFIG_HOME": str(self._home / ".config")}
        return super().run(argv, cwd=cwd, timeout=timeout, log_path=log_path, env_extra={**(env_extra or {}), **overrides})
