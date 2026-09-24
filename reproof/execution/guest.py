"""One root-owned guest agent, with candidates under a dedicated unprivileged UID."""
from __future__ import annotations

import ctypes
import hashlib
import math
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time

from reproloop.contracts.versions import digest, require
from reproloop.core import ContractError
from .artifacts import ArtifactError, BlobSet, receive_blobs, send_blobs
from .resources import ResourceError, validate_catalog
from .wire import ProtocolError

INSTALL_ROOT = Path("/Library/ReproLoopGuest")
MAX_LOG_BYTES = 1024 * 1024


class GuestError(RuntimeError):
    pass


class GuestOutputError(GuestError):
    pass


def assert_guest():
    """This executable guard is independent of environment variables/arguments."""
    try:
        if platform.system() != "Darwin" or os.getuid() != 0 or os.geteuid() != 0:
            raise GuestError("Owned macOS guest required")
        present, length = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        sysctl = ctypes.CDLL(None, use_errno=True).sysctlbyname
        sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                           ctypes.c_void_p, ctypes.c_size_t]
        sysctl.restype = ctypes.c_int
        if sysctl(b"kern.hv_vmm_present", ctypes.byref(present), ctypes.byref(length), None, 0) != 0 or present.value != 1:
            raise GuestError("Owned macOS guest required")
        info = INSTALL_ROOT.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GuestError("Trusted guest installation required")
    except (OSError, AttributeError):
        raise GuestError("Owned macOS guest required") from None


class GuestExecutor:
    def __init__(self, *, uid, gid):
        if type(uid) is not int or not 501 <= uid <= 60000 or type(gid) is not int or not 1 <= gid <= 60000:
            raise GuestError("Dedicated guest identity required")
        self.uid, self.gid = uid, gid

    def _terminate(self, process=None):
        assert_guest()
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        # setsid descendants leave the process group. The dedicated UID belongs
        # exclusively to this disposable guest job. Host VM stop remains the
        # independent and final containment boundary for every guest process.
        for _ in range(3):
            subprocess.run(["/usr/bin/pkill", "-KILL", "-u", str(self.uid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
            remaining = subprocess.run(["/usr/bin/pgrep", "-u", str(self.uid)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=2)
            if remaining.returncode == 1:
                return
            if remaining.returncode != 0:
                break
            time.sleep(0.05)
        raise GuestError("Guest process termination unconfirmed")

    def execute(self, blobs, recipe, cancel, *, on_started=None):
        assert_guest()
        recipe = validate_catalog([recipe])[0]
        if cancel.is_set():
            raise GuestError("Guest execution cancelled")
        self._terminate()
        parent = Path("/private/var/reproloop-jobs")
        parent.mkdir(mode=0o755, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GuestError("Guest job directory rejected")
        directory = Path(tempfile.mkdtemp(prefix="job-", dir=parent))
        process = None
        try:
            source, output = directory / "source", directory / "output"
            blobs.write_new(source)
            for name in ("output", "home", "tmp"):
                (directory / name).mkdir(mode=0o700)
            for current, directories, files in os.walk(directory):
                os.chown(current, self.uid, self.gid)
                for name in files:
                    os.chown(Path(current) / name, self.uid, self.gid)
            deadline = time.monotonic() + recipe["timeoutMs"] / 1000

            assert_guest()
            if cancel.is_set():
                raise GuestError("Guest execution cancelled")
            process = subprocess.Popen(
                [str(INSTALL_ROOT / "guest-run"), str(self.uid), str(self.gid),
                 str(math.ceil(recipe["timeoutMs"] / 1000) + 1), *recipe["argv"]],
                cwd=source, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C",
                     "HOME": str(directory / "home"), "TMPDIR": str(directory / "tmp"),
                     "REPROLOOP_OUTPUT_DIR": str(output)})
            os.set_blocking(process.stdout.fileno(), False)
            log, count, truncated, interrupted = hashlib.sha256(), 0, False, False
            readiness = bytearray()
            announced = False
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    interrupted = True
                    break
                readable, _, _ = select.select([process.stdout], [], [], 0.05)
                if readable:
                    raw = os.read(process.stdout.fileno(), min(65536, MAX_LOG_BYTES - count + 1))
                    count += len(raw)
                    if count > MAX_LOG_BYTES:
                        truncated = True
                        break
                    log.update(raw)
                    if on_started is not None and not announced and len(readiness) < 64:
                        readiness.extend(raw[:64 - len(readiness)])
                        if readiness.startswith(b"repro-probe-running\n"):
                            on_started()
                            announced = True
                if process.poll() is not None:
                    break
            self._terminate(process)
            code = process.returncode if not (interrupted or truncated) else 124
            result = {"exitCode": max(-255, min(255, code)), "outputTruncated": truncated,
                      "logDigest": log.hexdigest()}
            if cancel.is_set():
                raise GuestError("Guest execution cancelled")
            try:
                artifacts = (BlobSet.from_directory(output, recipe["outputPaths"],
                                                   max_bytes=recipe["maxOutputBytes"])
                             if code == 0 else None)
            except ArtifactError:
                raise GuestOutputError("Guest output rejected") from None
            return result, artifacts
        except (OSError, subprocess.SubprocessError, ArtifactError, ResourceError):
            raise GuestError("Guest execution failed") from None
        finally:
            try:
                self._terminate(process)
            finally:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                # This path exists only after the kernel/installation guard.
                # No guest cleanup reaches a host directory share.
                assert_guest()
                shutil.rmtree(directory)


def serve_one(channel, *, catalog, agent_digest, executor, probe=None):
    """Only this trusted composition root supplies the executor and catalog."""
    cancel, finished = threading.Event(), threading.Event()
    watcher = None
    code = "input-rejected"
    try:
        recipes = {item["id"]: item for item in validate_catalog(catalog)}
        channel.send("ready", {"agentDigest": agent_digest, "catalogDigest": digest(catalog)})
        first = channel.receive()
        on_started = None
        if first[0] == "probe":
            code = "recipe-rejected"
            require(probe is not None, "Trusted probe unavailable")
            mode = first[1]["mode"]
            source, recipe = probe.prepare(mode)
            on_started = lambda: channel.send("probe-started", {"mode": mode})
        else:
            source = receive_blobs(channel, prefix="input", first_message=first)
            code = "recipe-rejected"
            kind, request = channel.receive()
            require(kind == "run" and request["recipeId"] in recipes
                    and request["inputDigest"] == source.digest, "Recipe binding rejected")
            recipe = recipes[request["recipeId"]]

        def watch():
            try:
                channel.receive()  # The only valid next message is cancellation.
            except ProtocolError:
                pass
            if not finished.is_set():
                cancel.set()

        watcher = threading.Thread(target=watch, daemon=True, name="repro-guest-cancel")
        watcher.start()
        code = "execution-failed"
        result, artifacts = (executor.execute(source, recipe, cancel) if on_started is None else
                             executor.execute(source, recipe, cancel, on_started=on_started))
        if cancel.is_set():
            code = "cancelled"
            raise GuestError("Guest execution cancelled")
        finished.set()
        # Keep the receive direction alive until all output is sent: an EOF
        # poisons the complete channel. The host independently checks durable
        # cancellation again before publishing any completed result.
        if artifacts is not None:
            code = "output-rejected"
            require(set(path for path, _ in artifacts.entries) == set(recipe["outputPaths"])
                    and sum(len(raw) for _, raw in artifacts.entries) <= recipe["maxOutputBytes"],
                    "Output policy rejected")
            send_blobs(channel, artifacts, prefix="artifact")
        channel.send("result", result)
    except (ProtocolError, ArtifactError, ResourceError, ContractError, GuestError, OSError) as error:
        if type(error) is GuestOutputError:
            code = "output-rejected"
        try:
            channel.send("error", {"code": code})
        except ProtocolError:
            pass
    finally:
        finished.set()
        cancel.set()
        try:
            channel.sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        if watcher is not None:
            watcher.join(1)
