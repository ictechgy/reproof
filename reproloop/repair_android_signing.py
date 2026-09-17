"""Fixed Android APK signing and independent package/signature inspection.

Operator code registers one explicit private keystore capability.  Issue JSON,
candidate output, and AI responses cannot select tools, commands, key paths, or
passwords.  Tool output is bounded and never included in errors or evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
import zipfile

from . import contracts
from .execution.artifacts import BlobSet
from .execution.protocol import validate_signing_policy
from .execution.wire import MAX_TRANSFER_BYTES
from .repair_signing import (
    SignatureObservation, SigningContext, SigningFailureObservation,
    SigningObservation,
)


APK_PATH = "candidate.apk"
MAX_TOOL_BYTES = 64 * 1024
MAX_KEYSTORE_BYTES = 16 * 1024 * 1024
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CERTIFICATE_LINE = re.compile(
    rb"^Signer #([0-9]+) certificate SHA-256 digest: ([0-9A-Fa-f]{64})$",
    re.MULTILINE)
_SOURCE_STAMP_LINE = re.compile(
    rb"^Source Stamp Signer certificate SHA-256 digest:", re.MULTILINE)
_SCHEME_LINE = re.compile(
    rb"^Verified using (v[0-9.]+) scheme [^\r\n]*: (true|false)$",
    re.MULTILINE)
_PACKAGE_LINE = re.compile(rb"^package: ([A-Za-z0-9_.]+)$", re.MULTILINE)
_PERMISSION_LINE = re.compile(
    rb"^uses-permission: name='([A-Za-z0-9_.]+)'$", re.MULTILINE)
_PERMISSION = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_SCHEMES = ("v1", "v2", "v3", "v3.1", "v4")


class AndroidSigningError(RuntimeError):
    """Static adapter failure without tool output, paths, or secret material."""

    def __init__(self, code="android_signing_unavailable"):
        self.code = code
        super().__init__(code)


def _require(condition, code="android_signing_unavailable"):
    if not condition:
        raise AndroidSigningError(code)


def _sha256_file(path: Path, maximum: int) -> str:
    try:
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and not path.is_symlink()
                 and 0 < before.st_size <= maximum,
                 "android_signing_tool_invalid")
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(descriptor)
            _require((opened.st_dev, opened.st_ino, opened.st_size)
                     == (before.st_dev, before.st_ino, before.st_size),
                     "android_signing_tool_invalid")
            digest = hashlib.sha256()
            total = 0
            while total < opened.st_size:
                chunk = os.read(descriptor, min(1024 * 1024,
                                                opened.st_size - total))
                _require(bool(chunk), "android_signing_tool_invalid")
                total += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            _require((after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                     == (opened.st_size, opened.st_mtime_ns,
                         opened.st_ctime_ns),
                     "android_signing_tool_invalid")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    except AndroidSigningError:
        raise
    except OSError:
        raise AndroidSigningError("android_signing_tool_invalid") from None


@dataclass(frozen=True, slots=True)
class AndroidSigningTools:
    java: Path
    java_digest: str
    apksigner_jar: Path
    apksigner_jar_digest: str
    package_inspector: Path
    package_inspector_digest: str

    def __post_init__(self):
        for name in ("java", "apksigner_jar", "package_inspector"):
            selected = Path(getattr(self, name))
            _require(selected.is_absolute(), "android_signing_tool_invalid")
            try:
                path = selected.resolve(strict=True)
            except OSError:
                raise AndroidSigningError(
                    "android_signing_tool_invalid") from None
            object.__setattr__(self, name, path)
        for value in (self.java_digest, self.apksigner_jar_digest,
                      self.package_inspector_digest):
            _require(type(value) is str and _DIGEST.fullmatch(value) is not None,
                     "android_signing_tool_invalid")
        self.verify()

    def verify(self):
        for path, expected, executable in (
            (self.java, self.java_digest, True),
            (self.apksigner_jar, self.apksigner_jar_digest, False),
            (self.package_inspector, self.package_inspector_digest, True),
        ):
            try:
                info = path.lstat()
            except OSError:
                raise AndroidSigningError(
                    "android_signing_tool_invalid") from None
            _require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                     and (not executable or info.st_mode & 0o111)
                     and _sha256_file(path, 64 * 1024 * 1024) == expected,
                     "android_signing_tool_invalid")


@dataclass(frozen=True, slots=True)
class AndroidSigningIdentity:
    reference_id: str
    application_id: str
    package_name: str
    certificate_sha256: str
    signature_schemes: tuple[str, ...]
    permissions: tuple[str, ...]

    def __post_init__(self):
        try:
            contracts.validate_id(self.reference_id)
            contracts.validate_id(self.application_id)
            contracts.validate_digest(self.certificate_sha256)
        except contracts.ContractError:
            raise AndroidSigningError("android_signing_identity_invalid") from None
        _require(type(self.package_name) is str
                 and _PACKAGE.fullmatch(self.package_name) is not None,
                 "android_signing_identity_invalid")
        _require(type(self.signature_schemes) is tuple
                 and len(self.signature_schemes) == len(set(self.signature_schemes))
                 and set(self.signature_schemes) <= {"v1", "v2", "v3"}
                 and "v2" in self.signature_schemes
                 and tuple(sorted(self.signature_schemes))
                 == self.signature_schemes,
                 "android_signing_identity_invalid")
        _require(type(self.permissions) is tuple
                 and len(self.permissions) <= 256
                 and tuple(sorted(set(self.permissions))) == self.permissions
                 and all(type(item) is str
                         and _PERMISSION.fullmatch(item) is not None
                         for item in self.permissions),
                 "android_signing_identity_invalid")

    @property
    def signing_configuration(self):
        return {
            "schemaVersion": 1,
            "signatureSchemes": list(self.signature_schemes),
            "permissions": list(self.permissions),
        }

    @property
    def signing_configuration_digest(self):
        return contracts.digest(self.signing_configuration)


@dataclass(slots=True)
class _RegisteredMaterial:
    identity: AndroidSigningIdentity
    keystore: Path
    key_alias: str
    store_password: bytearray = field(repr=False)
    key_password: bytearray = field(repr=False)
    file_identity: tuple[int, int, int, int, int] = field(repr=False)

    def erase(self):
        for value in (self.store_password, self.key_password):
            value[:] = b"\0" * len(value)


class _OpenedMaterial:
    __slots__ = ("descriptor", "key_alias", "password_input")

    def __init__(self, descriptor, key_alias, store_password, key_password):
        self.descriptor = descriptor
        self.key_alias = key_alias
        self.password_input = bytearray(store_password + b"\n" +
                                        key_password + b"\n")

    def close(self):
        try:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
        finally:
            self.password_input[:] = b"\0" * len(self.password_input)


class AndroidSigningMaterialResolver:
    """Operator-owned in-memory map from an opaque policy ID to one key."""

    def __init__(self):
        self._lock = threading.RLock()
        self._materials = {}
        self._closed = False

    def __repr__(self):
        return (f"<{type(self).__name__} registered={len(self._materials)} "
                f"closed={self._closed}>")

    @staticmethod
    def _secret(value):
        _require(type(value) in (bytes, bytearray) and 1 <= len(value) <= 1024
                 and b"\0" not in value and b"\n" not in value
                 and b"\r" not in value,
                 "android_signing_material_invalid")
        return bytearray(value)

    def register(self, identity, *, keystore, key_alias, store_password,
                 key_password=None):
        _require(type(identity) is AndroidSigningIdentity
                 and type(key_alias) is str
                 and _ALIAS.fullmatch(key_alias) is not None,
                 "android_signing_material_invalid")
        path = Path(keystore)
        _require(path.is_absolute(), "android_signing_material_invalid")
        try:
            info = path.lstat()
        except OSError:
            raise AndroidSigningError("android_signing_material_invalid") from None
        _require(path.is_absolute() and stat.S_ISREG(info.st_mode)
                 and not path.is_symlink() and info.st_uid == os.getuid()
                 and info.st_nlink == 1 and info.st_mode & 0o077 == 0
                 and 0 < info.st_size <= MAX_KEYSTORE_BYTES,
                 "android_signing_material_invalid")
        store = key = material = None
        try:
            store = self._secret(store_password)
            key = self._secret(key_password if key_password is not None
                               else store_password)
            material = _RegisteredMaterial(
                identity, path, key_alias, store, key,
                (info.st_dev, info.st_ino, info.st_size,
                 info.st_mtime_ns, info.st_ctime_ns))
            with self._lock:
                _require(not self._closed
                         and identity.reference_id not in self._materials,
                         "android_signing_material_invalid")
                self._materials[identity.reference_id] = material
                material = None
        finally:
            if material is not None:
                material.erase()
            elif key is None and store is not None:
                store[:] = b"\0" * len(store)

    def require_registered(self, identity):
        """Check explicit in-memory registration without opening private material."""
        _require(type(identity) is AndroidSigningIdentity, 'android_signing_material_invalid')
        with self._lock:
            material = self._materials.get(identity.reference_id)
            _require(not self._closed and material is not None and material.identity == identity,
                     'android_signing_material_invalid')
        return identity

    def open(self, identity):
        _require(type(identity) is AndroidSigningIdentity,
                 "android_signing_material_invalid")
        with self._lock:
            material = self._materials.get(identity.reference_id)
            _require(not self._closed and material is not None
                     and material.identity == identity,
                     "android_signing_material_invalid")
            descriptor = None
            try:
                descriptor = os.open(
                    material.keystore,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0))
                info = os.fstat(descriptor)
            except OSError:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise AndroidSigningError(
                    "android_signing_material_invalid") from None
            current = (info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns)
            if (current != material.file_identity
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_nlink != 1
                    or info.st_mode & 0o077):
                os.close(descriptor)
                raise AndroidSigningError(
                    "android_signing_material_invalid")
            return _OpenedMaterial(
                descriptor, material.key_alias,
                material.store_password, material.key_password)

    def close(self):
        with self._lock:
            if self._closed:
                return
            for material in self._materials.values():
                material.erase()
            self._materials.clear()
            self._closed = True


class _Collector:
    def __init__(self, stream, maximum=MAX_TOOL_BYTES):
        self.stream = stream
        self.maximum = maximum
        self.data = bytearray()
        self.oversized = False
        self.thread = threading.Thread(
            target=self._read, name="repro-apksigner-output", daemon=True)

    def _read(self):
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    return
                if len(self.data) < self.maximum + 1:
                    remaining = self.maximum + 1 - len(self.data)
                    self.data.extend(chunk[:remaining])
                if len(self.data) > self.maximum:
                    self.oversized = True
        except OSError:
            self.oversized = True
        finally:
            try:
                self.stream.close()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    terminated: bool
    bounded: bool
    interrupted: bool


class _ProcessOwner:
    def __init__(self):
        self._lock = threading.RLock()
        self._processes = set()
        self._closed = False

    @property
    def active_processes(self):
        with self._lock:
            return len(self._processes)

    @staticmethod
    def _group_empty(identifier):
        try:
            os.killpg(identifier, 0)
            return False
        except ProcessLookupError:
            return True
        except OSError:
            return False

    @classmethod
    def _terminate(cls, process, *, deadline_monotonic=None):
        deadline_monotonic = (time.monotonic() + 3 if deadline_monotonic is None
                              else deadline_monotonic)
        for selected, wait_seconds in ((signal.SIGTERM, .5),
                                       (signal.SIGKILL, 2.0)):
            if process.poll() is not None:
                # No signal may use a PID after this owner has reaped it,
                # including normal-return and exception cleanup paths.
                return cls._group_empty(process.pid)
            if time.monotonic() >= deadline_monotonic:
                return process.poll() is not None and cls._group_empty(process.pid)
            try:
                os.killpg(process.pid, selected)
            except ProcessLookupError:
                pass
            except OSError:
                # A just-exited child can race the signal on macOS. A denied
                # signal never proves collection; reaping plus an empty group does.
                return process.poll() is not None and cls._group_empty(process.pid)
            deadline = min(deadline_monotonic, time.monotonic() + wait_seconds)
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            if process.poll() is not None and cls._group_empty(process.pid):
                return True
        try:
            process.wait(timeout=max(0, min(.1, deadline_monotonic - time.monotonic())))
        except (subprocess.TimeoutExpired, OSError):
            return False
        return cls._group_empty(process.pid)

    def run(self, arguments, *, work, input_bytes, pass_fds,
            cancellation, deadline_monotonic, watched_files=(),
            max_output_bytes=MAX_TOOL_BYTES):
        _require(type(arguments) is tuple and arguments
                 and all(type(item) is str and "\0" not in item
                         for item in arguments),
                 "android_signing_process_invalid")
        _require(callable(getattr(cancellation, "is_set", None))
                 and type(deadline_monotonic) in (int, float),
                 "android_signing_process_invalid")
        _require(type(max_output_bytes) is int
                 and 0 < max_output_bytes <= MAX_TRANSFER_BYTES,
                 "android_signing_process_invalid")
        _require(type(watched_files) is tuple and all(
            type(item) is tuple and len(item) == 2
            and isinstance(item[0], Path) and type(item[1]) is int
            and 0 < item[1] <= MAX_TRANSFER_BYTES
            for item in watched_files),
            "android_signing_process_invalid")
        process = None
        collectors = []
        interrupted = cancellation.is_set() or time.monotonic() >= deadline_monotonic
        if interrupted:
            raise AndroidSigningError(
                "android_signing_cancelled" if cancellation.is_set()
                else "android_signing_timeout")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(work),
        }
        try:
            with self._lock:
                _require(not self._closed, "android_signing_process_closed")
                process = subprocess.Popen(
                    arguments, cwd=work, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment, close_fds=True, pass_fds=pass_fds,
                    start_new_session=True)
                self._processes.add(process)
            collectors = [_Collector(process.stdout, max_output_bytes),
                          _Collector(process.stderr, max_output_bytes)]
            for collector in collectors:
                collector.thread.start()
            try:
                process.stdin.write(input_bytes)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()
            output_exceeded = False
            while process.poll() is None:
                if any(collector.oversized for collector in collectors):
                    output_exceeded = True
                    break
                for path, maximum in watched_files:
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        output_exceeded = True
                        break
                    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                            or info.st_size > maximum):
                        output_exceeded = True
                        break
                if output_exceeded:
                    break
                if self._closed or cancellation.is_set() or time.monotonic() >= deadline_monotonic:
                    interrupted = True
                    break
                time.sleep(.01)
            terminated = (self._terminate(process)
                          if interrupted or output_exceeded
                          else self._group_empty(process.pid))
            if process.poll() is None:
                terminated = self._terminate(process)
            else:
                process.wait()
                if not self._group_empty(process.pid):
                    terminated = self._terminate(process)
            for collector in collectors:
                collector.thread.join(2)
                if collector.thread.is_alive():
                    terminated = False
            bounded = all(not item.oversized for item in collectors)
            result = _ProcessResult(
                process.returncode,
                bytes(collectors[0].data), bytes(collectors[1].data),
                terminated, bounded, interrupted)
            if terminated:
                with self._lock:
                    self._processes.discard(process)
            return result
        except (OSError, ValueError, subprocess.SubprocessError):
            if process is not None and self._terminate(process):
                with self._lock:
                    self._processes.discard(process)
            raise AndroidSigningError(
                "android_signing_process_invalid") from None

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic() + 5 if deadline_monotonic is None else deadline_monotonic
        # Seal dispatch even when a Popen call still holds the registration
        # lock. Failure to collect that call retains this owner for a retry.
        self._closed = True
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            processes = tuple(self._processes)
        finally:
            self._lock.release()
        for process in processes:
            try:
                # A reaped leader no longer protects its PID from reuse.
                # A remaining group cannot be signalled from that PID alone.
                clean = (self._group_empty(process.pid) if process.poll() is not None
                         else self._terminate(process, deadline_monotonic=deadline))
            except OSError:
                clean = False
            if clean:
                with self._lock:
                    self._processes.discard(process)
        return self.active_processes == 0


def _private_directory(path: Path):
    path = Path(path)
    _require(path.is_absolute(), "android_signing_work_invalid")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError:
        raise AndroidSigningError("android_signing_work_invalid") from None
    _require(stat.S_ISDIR(info.st_mode) and not path.is_symlink()
             and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
             "android_signing_work_invalid")
    return path


def _new_work(root: Path, identifier: str):
    root = _private_directory(root)
    work = root / identifier
    try:
        work.mkdir(mode=0o700)
    except OSError:
        raise AndroidSigningError("android_signing_work_invalid") from None
    return work


def _write_apk(work: Path, body: bytes):
    descriptor = None
    directory = None
    try:
        directory = os.open(
            work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = os.open(
            APK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0), 0o600,
            dir_fd=directory)
        offset = 0
        while offset < len(body):
            written = os.write(descriptor, body[offset:offset + 1024 * 1024])
            _require(written > 0, "android_signing_work_invalid")
            offset += written
        os.fsync(descriptor)
    except (OSError, AndroidSigningError):
        raise AndroidSigningError("android_signing_work_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    try:
        _sync(work)
    except OSError:
        raise AndroidSigningError("android_signing_work_invalid") from None
    return work / APK_PATH


def _sync(path):
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_apk(path: Path, maximum: int):
    try:
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and not path.is_symlink()
                 and before.st_uid == os.getuid()
                 and 0 < before.st_size <= maximum,
                 "android_signing_output_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            _require((opened.st_dev, opened.st_ino, opened.st_size)
                     == (before.st_dev, before.st_ino, before.st_size),
                     "android_signing_output_invalid")
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(descriptor,
                                min(1024 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
            _require(len(data) == opened.st_size <= maximum
                     and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                     == (opened.st_size, opened.st_mtime_ns,
                         opened.st_ctime_ns),
                     "android_signing_output_invalid")
            return bytes(data)
        finally:
            os.close(descriptor)
    except AndroidSigningError:
        raise
    except OSError:
        raise AndroidSigningError("android_signing_output_invalid") from None


def _cleanup_work(root: Path, work: Path, allowed):
    try:
        children = list(work.iterdir())
        if {item.name for item in children} != set(allowed):
            return False
        for item in children:
            info = item.lstat()
            if (not stat.S_ISREG(info.st_mode) or item.is_symlink()
                    or info.st_uid != os.getuid()):
                return False
        for item in children:
            item.unlink()
        _sync(work)
        work.rmdir()
        _sync(root)
        return True
    except OSError:
        return False


def _cleanup_known_work(root: Path, work: Path, allowed):
    try:
        children = list(work.iterdir())
        if not {item.name for item in children} <= set(allowed):
            return False
        for item in children:
            info = item.lstat()
            if (not stat.S_ISREG(info.st_mode) or item.is_symlink()
                    or info.st_uid != os.getuid()):
                return False
        for item in children:
            item.unlink()
        _sync(work)
        work.rmdir()
        _sync(root)
        return True
    except OSError:
        return False


def _artifact_body(artifacts, maximum):
    _require(type(artifacts) is BlobSet and len(artifacts.entries) == 1
             and artifacts.entries[0][0] == APK_PATH
             and 0 < len(artifacts.entries[0][1]) <= maximum,
             "android_signing_artifact_invalid")
    return artifacts.entries[0][1]


def _structural_apk(body, maximum):
    if type(body) is not bytes or not 0 < len(body) <= maximum:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 32768:
                return False
            names = set()
            total = 0
            for entry in entries:
                name = entry.filename
                if (type(name) is not str or not name or name.startswith("/")
                        or "\\" in name or "\0" in name
                        or any(part in {"", ".", ".."}
                               for part in name.rstrip("/").split("/"))
                        or name.casefold() in names or entry.flag_bits & 0x1):
                    return False
                names.add(name.casefold())
                if entry.is_dir():
                    continue
                if not 0 <= entry.compress_size <= maximum:
                    return False
                if not 0 <= entry.file_size <= maximum:
                    return False
                if (entry.compress_size == 0 and entry.file_size > 0
                        or entry.compress_size > 0
                        and entry.file_size > entry.compress_size * 200):
                    return False
                total += entry.file_size
                if total > 4 * maximum:
                    return False
            return "androidmanifest.xml" in names
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def _manifest_measurement(owner, tools, source, work, *, cancellation,
                          deadline_monotonic):
    measured = owner.run((
        str(tools.package_inspector), "dump", "permissions", str(source),
    ), work=work, input_bytes=b"", pass_fds=(),
        cancellation=cancellation, deadline_monotonic=deadline_monotonic)
    _require(measured.terminated, "android_signing_lifecycle_unknown")
    if measured.interrupted:
        raise AndroidSigningError(
            "android_signing_cancelled" if cancellation.is_set()
            else "android_signing_timeout")
    if not measured.bounded or measured.returncode != 0:
        return None, None
    lines = [line for line in measured.stdout.splitlines() if line]
    if not lines:
        return None, None
    package_match = _PACKAGE_LINE.fullmatch(lines[0])
    if package_match is None:
        return None, None
    permissions = []
    for line in lines[1:]:
        matched = _PERMISSION_LINE.fullmatch(line)
        if matched is None:
            return None, None
        try:
            permission = matched.group(1).decode("ascii")
        except UnicodeError:
            return None, None
        if _PERMISSION.fullmatch(permission) is None:
            return None, None
        permissions.append(permission)
    try:
        package = package_match.group(1).decode("ascii")
    except UnicodeError:
        return None, None
    if (len(permissions) > 256 or len(permissions) != len(set(permissions))
            or _PACKAGE.fullmatch(package) is None):
        return None, None
    return package, tuple(sorted(permissions))


def _validated_policy(identity, policy_document):
    try:
        policy = validate_signing_policy(policy_document)
    except (contracts.ContractError, TypeError, ValueError):
        raise AndroidSigningError("android_signing_policy_invalid") from None
    _require(type(identity) is AndroidSigningIdentity
             and policy["platform"] == "android"
             and policy["applicationId"] == identity.application_id
             and policy["identityReferenceId"] == identity.reference_id
             and policy["entitlementsDigest"]
             == identity.signing_configuration_digest,
             "android_signing_policy_invalid")
    return policy, contracts.digest(policy)


def _validate_context(context, identity, policy_digest, body,
                      *, signed_required):
    _require(type(context) is SigningContext
             and context.application_id == identity.application_id
             and context.signing_policy_digest == policy_digest
             and type(context.nonce) is str and 1 <= len(context.nonce) <= 128,
             "android_signing_context_invalid")
    try:
        contracts.validate_id(context.operation_id)
        for value in (context.repair_plan_digest, context.project_digest,
                      context.source_digest, context.unsigned_artifact_digest,
                      context.signing_policy_digest):
            contracts.validate_digest(value)
    except contracts.ContractError:
        raise AndroidSigningError("android_signing_context_invalid") from None
    measured = hashlib.sha256(body).hexdigest()
    if signed_required:
        _require(context.signed_artifact_digest == measured,
                 "android_signing_context_invalid")
    else:
        _require(context.signed_artifact_digest is None
                 and context.unsigned_artifact_digest == measured,
                 "android_signing_context_invalid")


class AndroidApkSigner:
    """Fixed apksigner callback suitable for ``TrustedSigningSupervisor``."""

    def __init__(self, tools, resolver, identity, policy_document, work_root,
                 *, max_apk_bytes=MAX_TRANSFER_BYTES):
        _require(type(tools) is AndroidSigningTools
                 and type(resolver) is AndroidSigningMaterialResolver,
                 "android_signing_configuration_invalid")
        _require(type(max_apk_bytes) is int
                 and 1 <= max_apk_bytes <= MAX_TRANSFER_BYTES,
                 "android_signing_configuration_invalid")
        policy, policy_digest = _validated_policy(identity, policy_document)
        self.tools, self.resolver, self.identity = tools, resolver, identity
        self.policy, self.policy_digest = policy, policy_digest
        self.work_root = _private_directory(Path(work_root))
        self.max_apk_bytes = max_apk_bytes
        self._owner = _ProcessOwner()

    @property
    def active_processes(self):
        return self._owner.active_processes

    def __call__(self, context, artifacts, *, cancellation,
                 deadline_monotonic):
        body = _artifact_body(artifacts, self.max_apk_bytes)
        _validate_context(context, self.identity, self.policy_digest, body,
                          signed_required=False)
        work = None
        try:
            self.tools.verify()
            _require(not cancellation.is_set()
                     and time.monotonic() < deadline_monotonic,
                     "android_signing_cancelled")
            work = _new_work(self.work_root, context.digest)
            source = _write_apk(work, body)
            output = work / "signed.apk"
            package, permissions = _manifest_measurement(
                self._owner, self.tools, source, work,
                cancellation=cancellation,
                deadline_monotonic=deadline_monotonic)
            _require(package == self.identity.package_name
                     and permissions == self.identity.permissions,
                     "android_signing_artifact_invalid")
            material = self.resolver.open(self.identity)
            scheme_arguments = tuple(
                value for scheme in ("v1", "v2", "v3")
                for value in (f"--{scheme}-signing-enabled",
                              "true" if scheme in self.identity.signature_schemes
                              else "false"))
            try:
                result = self._owner.run((
                    str(self.tools.java), "-Xmx256m", "-jar",
                    str(self.tools.apksigner_jar), "sign",
                    "--ks", f"/dev/fd/{material.descriptor}",
                    "--ks-key-alias", material.key_alias,
                    "--ks-pass", "stdin", "--key-pass", "stdin",
                    *scheme_arguments, "--v4-signing-enabled", "false",
                    "--out", str(output), str(source),
                ), work=work, input_bytes=material.password_input,
                    pass_fds=(material.descriptor,), cancellation=cancellation,
                    deadline_monotonic=deadline_monotonic,
                    watched_files=((output, self.max_apk_bytes),))
            finally:
                material.close()
            _require(result.terminated, "android_signing_lifecycle_unknown")
            if result.interrupted:
                raise AndroidSigningError(
                    "android_signing_cancelled" if cancellation.is_set()
                    else "android_signing_timeout")
            _require(result.bounded and result.returncode == 0,
                     "android_signing_failed")
            signed = _read_apk(output, self.max_apk_bytes)
            signed_digest = hashlib.sha256(signed).hexdigest()
            _require(signed_digest != context.unsigned_artifact_digest,
                     "android_signing_output_invalid")
            signed_artifacts = BlobSet(((APK_PATH, signed),))
            evidence = contracts.digest({
                "kind": "android-fixed-apksigner",
                "contextDigest": context.digest,
                "inputDigest": context.unsigned_artifact_digest,
                "outputDigest": signed_digest,
                "artifactSetDigest": signed_artifacts.digest,
                "policyDigest": self.policy_digest,
                "javaDigest": self.tools.java_digest,
                "apksignerJarDigest": self.tools.apksigner_jar_digest,
            })
            cleaned = _cleanup_work(
                self.work_root, work, {APK_PATH, "signed.apk"})
            return SigningObservation(
                context.digest, signed_artifacts, evidence, True, cleaned)
        except AndroidSigningError as error:
            terminated = self._owner.active_processes == 0
            cleaned = (work is None or terminated and _cleanup_known_work(
                self.work_root, work, {APK_PATH, "signed.apk"}))
            if not terminated or not cleaned:
                raise
            code = ("cancelled" if error.code == "android_signing_cancelled"
                    else "signing_timeout"
                    if error.code == "android_signing_timeout"
                    else "artifact_invalid"
                    if error.code in {"android_signing_artifact_invalid",
                                      "android_signing_output_invalid"}
                    else "signing_failed")
            evidence = contracts.digest({
                "kind": "android-fixed-apksigner-failure",
                "contextDigest": context.digest,
                "inputDigest": context.unsigned_artifact_digest,
                "policyDigest": self.policy_digest,
                "javaDigest": self.tools.java_digest,
                "apksignerJarDigest": self.tools.apksigner_jar_digest,
                "code": code,
            })
            return SigningFailureObservation(
                context.digest, code, evidence, True, True)

    def close(self, *, deadline_monotonic=None):
        return self._owner.close(deadline_monotonic=deadline_monotonic)


class AndroidApkInspector:
    """Independent apksigner/aapt2 verifier for signed candidate APK bytes."""

    def __init__(self, tools, identity, policy_document, work_root, *,
                 max_apk_bytes=MAX_TRANSFER_BYTES, timeout_seconds=30):
        _require(type(tools) is AndroidSigningTools
                 and type(max_apk_bytes) is int
                 and 1 <= max_apk_bytes <= MAX_TRANSFER_BYTES
                 and type(timeout_seconds) in (int, float)
                 and 0 < timeout_seconds <= 120,
                 "android_signing_configuration_invalid")
        policy, policy_digest = _validated_policy(identity, policy_document)
        self.tools, self.identity = tools, identity
        self.policy, self.policy_digest = policy, policy_digest
        self.work_root = _private_directory(Path(work_root))
        self.max_apk_bytes = max_apk_bytes
        self.timeout_seconds = timeout_seconds
        self._owner = _ProcessOwner()

    @property
    def active_processes(self):
        return self._owner.active_processes

    def _inspect(self, body, work, *, cancellation, deadline_monotonic):
        source = _write_apk(work, body)
        signature = self._owner.run((
            str(self.tools.java), "-Xmx256m", "-jar",
            str(self.tools.apksigner_jar), "verify", "--verbose",
            "--print-certs", str(source),
        ), work=work, input_bytes=b"", pass_fds=(),
            cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)
        _require(signature.terminated,
                 "android_signing_lifecycle_unknown")
        if signature.interrupted:
            raise AndroidSigningError(
                "android_signing_cancelled" if cancellation.is_set()
                else "android_signing_timeout")
        certificates = [(number, digest.lower()) for number, digest in
                        _CERTIFICATE_LINE.findall(signature.stdout)]
        scheme_rows = _SCHEME_LINE.findall(signature.stdout)
        schemes = None
        if (len(scheme_rows) == len(_SCHEMES)
                and len({row[0] for row in scheme_rows}) == len(_SCHEMES)):
            try:
                scheme_values = {
                    name.decode("ascii"): enabled == b"true"
                    for name, enabled in scheme_rows
                }
            except UnicodeError:
                scheme_values = {}
            if set(scheme_values) == set(_SCHEMES):
                schemes = tuple(sorted(
                    name for name, enabled in scheme_values.items()
                    if enabled))
        valid = (signature.bounded and signature.returncode == 0
                 and certificates == [(b"1", self.identity.certificate_sha256.encode("ascii"))]
                 and _SOURCE_STAMP_LINE.search(signature.stdout) is None
                 and schemes == self.identity.signature_schemes)
        package, permissions = _manifest_measurement(
            self._owner, self.tools, source, work,
            cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)
        valid = (valid and package == self.identity.package_name
                 and permissions == self.identity.permissions
                 and contracts.digest({
                     "schemaVersion": 1,
                     "signatureSchemes": list(schemes or ()),
                     "permissions": list(permissions or ()),
                 }) == self.identity.signing_configuration_digest)
        return valid, package, schemes, permissions

    def __call__(self, context, artifacts, *, cancellation,
                 deadline_monotonic):
        body = _artifact_body(artifacts, self.max_apk_bytes)
        _validate_context(context, self.identity, self.policy_digest, body,
                          signed_required=True)
        work = None
        try:
            self.tools.verify()
            work = _new_work(self.work_root, context.digest)
            valid, package, schemes, permissions = self._inspect(
                body, work, cancellation=cancellation,
                deadline_monotonic=deadline_monotonic)
            cleaned = _cleanup_work(self.work_root, work, {APK_PATH})
            evidence = contracts.digest({
                "kind": "android-independent-apk-inspection",
                "contextDigest": context.digest,
                "artifactDigest": hashlib.sha256(body).hexdigest(),
                "policyDigest": self.policy_digest,
                "package": package,
                "signatureSchemes": list(schemes or ()),
                "permissions": list(permissions or ()),
                "certificateDigest": self.identity.certificate_sha256,
                "javaDigest": self.tools.java_digest,
                "apksignerJarDigest": self.tools.apksigner_jar_digest,
                "packageInspectorDigest": self.tools.package_inspector_digest,
                "valid": valid,
            })
            return SignatureObservation(
                context.digest, valid, evidence, True, cleaned)
        except AndroidSigningError as error:
            terminated = self._owner.active_processes == 0
            cleaned = (work is None or terminated and _cleanup_known_work(
                self.work_root, work, {APK_PATH}))
            if not terminated or not cleaned:
                raise
            code = ("cancelled" if error.code == "android_signing_cancelled"
                    else "signing_timeout"
                    if error.code == "android_signing_timeout"
                    else "signature_invalid")
            evidence = contracts.digest({
                "kind": "android-independent-apk-inspection-failure",
                "contextDigest": context.digest,
                "artifactDigest": hashlib.sha256(body).hexdigest(),
                "policyDigest": self.policy_digest,
                "javaDigest": self.tools.java_digest,
                "apksignerJarDigest": self.tools.apksigner_jar_digest,
                "packageInspectorDigest": self.tools.package_inspector_digest,
                "code": code,
            })
            return SigningFailureObservation(
                context.digest, code, evidence, True, True)

    def accepts(self, artifacts):
        try:
            body = _artifact_body(artifacts, self.max_apk_bytes)
            return _structural_apk(body, self.max_apk_bytes)
        except (AndroidSigningError, OSError, ValueError):
            return False

    def close(self, *, deadline_monotonic=None):
        return self._owner.close(deadline_monotonic=deadline_monotonic)


__all__ = [
    "AndroidApkInspector", "AndroidApkSigner", "AndroidSigningError",
    "AndroidSigningIdentity", "AndroidSigningMaterialResolver",
    "AndroidSigningTools", "APK_PATH",
]
