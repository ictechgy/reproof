"""Fixed Apple Virtualization helper supervisor; never starts candidate host code."""
from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import threading
import time

from .wire import ProtocolError, canonical, decode_json
from .artifacts import ArtifactError, open_directory
from .journal import OwnedRun


class NativeError(RuntimeError):
    pass


EVENTS = frozenset({"configured-no-network-no-shares", "started", "guest-connected", "stopped",
                    "configuration-rejected", "start-failed", "stop-unconfirmed"})


class NativeVM:
    """An instance owns one process, byte channel, and observed VM lifecycle."""
    def __init__(self, bundle, run, *, deadline, cancel):
        if type(run) is not OwnedRun or run.finished:
            raise NativeError("Owned VM operation required")
        directory = run.directory
        self.deadline, self.cancel = deadline, cancel
        self.process = None
        self._events = []
        self._condition = threading.Condition()
        self._failed = False
        self._start_failed = False
        self._reader = None
        self.channel, child_channel = socket.socketpair()
        directory_fd = termination_fd = None
        try:
            directory_fd = open_directory(directory)
            termination_fd = os.open("termination.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory_fd)
            settings = bundle.metadata["environment"]["resources"]
            config = {"schemaVersion": 1, "cpuCount": settings["cpuCount"],
                      "memoryBytes": settings["memoryMiB"] * 1024 ** 2,
                      "disk": str(directory / "disk.img"), "auxiliary": str(directory / "auxiliary.bin"),
                      "hardware": str(bundle.path("hardware")), "machine": str(bundle.path("machine")),
                      "toolchain": str(bundle.path("toolchain")), "channelFD": child_channel.fileno(), "port": 4050,
                      "timeoutMs": max(1, min(86400000, int((deadline - time.monotonic()) * 1000))),
                      "terminationFD": termination_fd, "runDirectoryFD": directory_fd,
                      "operationId": run.operation_id, "requestDigest": run.request_digest}
            if cancel.is_set() or time.monotonic() >= deadline:
                raise NativeError("VM start interrupted")
            self.process = subprocess.Popen(
                [str(bundle.path("helper"))], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, close_fds=True,
                pass_fds=(child_channel.fileno(), termination_fd, directory_fd), start_new_session=True,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"})
            os.set_blocking(self.process.stdin.fileno(), False)
            self._reader = threading.Thread(target=self._read_events, daemon=True, name="repro-vm-events")
            self._reader.start()
            self._write(canonical(config) + b"\n", deadline)
        except (OSError, NativeError, ProtocolError, ArtifactError):
            # Construction must not hide an uncertain launched VM. The instance
            # remains usable for shutdown and conservative journal quarantine.
            self._start_failed = True
        finally:
            child_channel.close()
            if termination_fd is not None:
                os.close(termination_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _read_events(self):
        try:
            while True:
                raw = self.process.stdout.readline(513)
                if not raw:
                    break
                if len(raw) > 512 or not raw.endswith(b"\n"):
                    raise NativeError("Invalid VM lifecycle record")
                record = decode_json(raw)
                if (type(record) is not dict or set(record) != {"event"}
                        or type(record["event"]) is not str or record["event"] not in EVENTS):
                    raise NativeError("Invalid VM lifecycle record")
                with self._condition:
                    if len(self._events) >= 16 or record["event"] in self._events:
                        raise NativeError("Duplicate VM lifecycle record")
                    self._events.append(record["event"])
                    self._condition.notify_all()
        except (OSError, ValueError, ProtocolError, NativeError):
            self._failed = True
        finally:
            with self._condition:
                self._condition.notify_all()

    def _write(self, raw, deadline, *, allow_cancelled=False):
        if self.process is None or self.process.poll() is not None:
            raise NativeError("VM owner unavailable")
        offset = 0
        while offset < len(raw):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (self.cancel.is_set() and not allow_cancelled):
                raise NativeError("VM control deadline exceeded")
            _, writable, _ = select.select([], [self.process.stdin], [], min(remaining, 0.05))
            if writable:
                if time.monotonic() >= deadline or (self.cancel.is_set() and not allow_cancelled):
                    raise NativeError("VM control interrupted")
                try:
                    count = os.write(self.process.stdin.fileno(), raw[offset:])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise NativeError("VM control unavailable")
                offset += count

    def wait_ready(self):
        with self._condition:
            while "guest-connected" not in self._events:
                if (self._failed or self._start_failed or self.process is None or self.process.poll() is not None
                        or self.cancel.is_set() or time.monotonic() >= self.deadline
                        or any(item in self._events for item in ("configuration-rejected", "start-failed", "stopped"))):
                    raise NativeError("VM readiness unavailable")
                self._condition.wait(min(0.05, self.deadline - time.monotonic()))
            if "configured-no-network-no-shares" not in self._events or self._failed or self._start_failed:
                raise NativeError("VM configuration unconfirmed")

    @property
    def evidence(self):
        with self._condition:
            return {"configured": "configured-no-network-no-shares" in self._events,
                    "started": "started" in self._events,
                    "connected": "guest-connected" in self._events,
                    "stopped": "stopped" in self._events and not self._failed}

    def stop(self, *, timeout=10):
        deadline = time.monotonic() + timeout
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    self._write(b"stop\n", min(deadline, time.monotonic() + 1), allow_cancelled=True)
                except (OSError, ValueError, NativeError):
                    pass
                # EOF on the separate channel is an additional stop signal to
                # the native owner, including a blocked/invalid protocol stream.
                try:
                    self.channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                with self._condition:
                    while self.process.poll() is None and time.monotonic() < deadline:
                        self._condition.wait(min(0.05, deadline - time.monotonic()))
        finally:
            self.channel.close()
            if self.process is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
                if self._reader is not None:
                    self._reader.join(1)
                self.process.stdin.close()
                self.process.stdout.close()
        with self._condition:
            # A dead helper alone is not proof. Pre-start rejection is the only
            # exception: the native main never invoked vm.start in that path.
            return (self.process is None or not self._failed and
                    ("stopped" in self._events or "configuration-rejected" in self._events))
