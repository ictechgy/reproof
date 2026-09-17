"""Bounded local bundle IO, hashes, and exclusive device leases."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from contextlib import contextmanager
from .core import ContractError, compile_capture, digest, require

MAX_JSON = 20 * 1024 * 1024
MAX_APK = 150 * 1024 * 1024
PACKAGE = "io.reproloop.sample"
AUTHORITY_MARKER_VERSION = 1
_AUTHORITY_STATES = frozenset({"shared", "rollback-blocked", "legacy-allowed"})


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_JSON,
            "JSON artifact missing, linked or oversized")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("Non-finite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("Malformed JSON artifact") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _require_profile(app_profile):
    if app_profile is None:
        return None
    from .android_profile import AndroidAppProfile
    require(isinstance(app_profile, AndroidAppProfile), "Invalid Android app profile")
    return app_profile


def create_bundle(capture, oracle, apk, destination, source_proof=None, capture_method="imported", app_profile=None, diagnostics=None):
    app_profile = _require_profile(app_profile)
    compile_capture(capture, oracle, app_profile=app_profile)  # Fail before persisting untrusted recordings.
    auto_capture = app_profile is not None and app_profile.data.get('captureMode') == 'debug_receiver'
    if auto_capture:
        from .instrumentation_diagnostics import validate_diagnostics
        diagnostics = validate_diagnostics(diagnostics, capture, app_profile)
    else:
        require(diagnostics is None, 'Unconfigured diagnostic attachment')
    apk = Path(apk)
    require(apk.is_file() and not apk.is_symlink() and 0 < apk.stat().st_size <= MAX_APK, "Invalid APK artifact")
    destination = Path(destination)
    require(not destination.exists(), "Bundle destination already exists")
    destination.mkdir(parents=True, mode=0o700)
    write_json(destination / "capture.json", capture)
    write_json(destination / "oracle.json", oracle)
    if app_profile is not None:
        write_json(destination / "app-profile.json", app_profile.data)
    if auto_capture:
        write_json(destination / 'diagnostics.json', diagnostics)
    with apk.open("rb") as src, (destination / "original.apk").open("xb") as dst:
        os.chmod(destination / "original.apk", 0o600)
        while chunk := src.read(1024 * 1024): dst.write(chunk)
    artifact_names = ("capture.json", "oracle.json", "original.apk")
    if app_profile is not None:
        artifact_names += ("app-profile.json",)
        if auto_capture:artifact_names += ('diagnostics.json',)
        manifest = {"schemaVersion": 2, "package": app_profile.data["package"],
                    "appProfileDigest": app_profile.digest,
                    "files": {n: sha_file(destination / n) for n in artifact_names},
                    "sourceProof": source_proof, "captureMethod": capture_method}
    else:
        manifest = {"schemaVersion": 1, "package": PACKAGE,
                    "files": {n: sha_file(destination / n) for n in artifact_names},
                    "sourceProof": source_proof, "captureMethod": capture_method}
    write_json(destination / "manifest.json", manifest)
    return load_bundle(destination, app_profile=app_profile)


def load_bundle(path, app_profile=None):
    app_profile = _require_profile(app_profile)
    path = Path(path).resolve()
    m = read_json(path / "manifest.json")
    require(isinstance(m, dict) and type(m.get("schemaVersion")) is int,
            "Unsupported bundle or package")
    if m.get("schemaVersion") == 1:
        require(app_profile is None and m.get("package") == PACKAGE,
                "Unsupported bundle or package")
        expected_files = {"capture.json", "oracle.json", "original.apk"}
    elif m.get("schemaVersion") == 2:
        require(app_profile is not None and m.get("package") == app_profile.data["package"]
                and m.get("appProfileDigest") == app_profile.digest,
                "Bundle requires the matching trusted app profile")
        expected_files = {"capture.json", "oracle.json", "original.apk", "app-profile.json"}
        if app_profile.data.get('captureMode') == 'debug_receiver':expected_files.add('diagnostics.json')
    else:
        raise ContractError("Unsupported bundle or package")
    require(isinstance(m.get("files"), dict) and set(m["files"]) == expected_files,
            "Unexpected artifact list")
    for name, expected in m["files"].items():
        f = path / name
        limit = MAX_APK if name.endswith('.apk') else 1024*1024 if name == 'diagnostics.json' else MAX_JSON
        require(f.is_file() and not f.is_symlink() and f.stat().st_size <= limit,
                "Missing or invalid bundle artifact")
        require(sha_file(f) == expected, "Bundle integrity mismatch")
    if app_profile is not None:
        from .android_profile import validate_app_profile
        embedded = validate_app_profile(read_json(path / "app-profile.json"))
        require(embedded.digest == app_profile.digest
                and embedded.native_digest == app_profile.native_digest,
                "Embedded app profile does not match the trusted profile")
    capture = read_json(path / "capture.json"); oracle = read_json(path / "oracle.json")
    scenario = compile_capture(capture, oracle, app_profile=app_profile)
    diagnostics = None
    if 'diagnostics.json' in expected_files:
        from .instrumentation_diagnostics import validate_diagnostics
        diagnostics = validate_diagnostics(read_json(path / 'diagnostics.json'), capture, app_profile)
        scenario.pop('scenarioDigest')
        scenario['diagnostics'] = diagnostics
        scenario['scenarioDigest'] = digest(scenario)
    return {"path": path, "manifest": m, "manifestDigest": digest(m), "capture": capture,
            "scenario": scenario, "oracle": oracle, "apk": path / "original.apk",
            "app_profile": app_profile, 'diagnostics': diagnostics}


class Lease:
    """Kernel-backed local lease; crashes release the lock without stale-lock takeover."""
    def __init__(self, serial, directory=None, *, authority_root=None):
        self.directory = Path(directory or (Path(tempfile.gettempdir()) / f"reproloop-leases-{os.getuid()}"))
        self.key = hashlib.sha256(serial.encode()).hexdigest()
        require(authority_root is None or (isinstance(authority_root, str)
                and re.fullmatch(r"[0-9a-f]{64}", authority_root)),
                "Invalid authority root")
        self.authority_root = authority_root
        self.marker = self.directory / (self.key + ".authority.json")
        self.file = None

    def _read_marker(self):
        if not self.marker.exists() and not self.marker.is_symlink():
            return None
        require(self.marker.is_file() and not self.marker.is_symlink()
                and self.marker.stat().st_uid == os.getuid()
                and self.marker.stat().st_size <= 1024,
                "Invalid authority cutover marker")
        try:
            value = json.loads(self.marker.read_text(encoding="utf-8"),
                               object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise ContractError("Invalid authority cutover marker") from None
        require(isinstance(value, dict)
                and set(value) == {"version", "state", "authorityRoot"}
                and type(value["version"]) is int
                and value["version"] == AUTHORITY_MARKER_VERSION
                and value["state"] in _AUTHORITY_STATES
                and isinstance(value["authorityRoot"], str)
                and re.fullmatch(r"[0-9a-f]{64}", value["authorityRoot"]),
                "Invalid authority cutover marker")
        return value

    def _check_cutover(self):
        marker = self._read_marker()
        if marker is None or marker["state"] == "legacy-allowed":
            return
        if self.authority_root is None:
            raise ContractError("Legacy controller cutover is blocked")
        require(marker["authorityRoot"] == self.authority_root,
                "Canonical authority root mismatch")

    def check_authority(self):
        """Inspect authority metadata without taking an execution lease."""
        if not self.directory.exists() and not self.directory.is_symlink():
            return
        info = self.directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not self.directory.is_symlink()
                and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                "Unsafe lease directory")
        self._check_cutover()

    def mark_authority(self, state):
        require(self.file is not None and self.authority_root is not None
                and state in _AUTHORITY_STATES,
                "Invalid authority cutover transition")
        value = {"version": AUTHORITY_MARKER_VERSION, "state": state,
                 "authorityRoot": self.authority_root}
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        fd, temporary = tempfile.mkstemp(prefix=".authority-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary, self.marker)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.directory.is_symlink() and self.directory.stat().st_uid == os.getuid(), "Unsafe lease directory")
        directory_fd = os.open(
            self.directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        lock_fd = None
        try:
            directory_stat = os.fstat(directory_fd)
            require(stat.S_ISDIR(directory_stat.st_mode) and directory_stat.st_uid == os.getuid(),
                    "Unsafe lease directory")
            self._directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
            lock_fd = os.open(
                self.key + ".lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            lock_stat = os.fstat(lock_fd)
            require(stat.S_ISREG(lock_stat.st_mode) and lock_stat.st_uid == os.getuid()
                    and lock_stat.st_nlink == 1, "Unsafe lease file")
            self.file = os.fdopen(lock_fd, "r+")
            lock_fd = None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(directory_fd)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close(); self.file = None
            raise ContractError("Device is already leased") from exc
        try:
            self._check_cutover()
        except BaseException:
            fcntl.flock(self.file, fcntl.LOCK_UN);self.file.close();self.file=None
            raise
        self.file.seek(0); self.file.truncate(); self.file.write(str(os.getpid())); self.file.flush()
        return self

    def __exit__(self, *_):
        if self.file:
            # flock follows the open file description. An inherited native
            # owner must retain it until its last descriptor actually closes.
            self.file.close(); self.file = None

    @contextmanager
    def borrow_descriptor(self):
        """Duplicate the original held lock, never a newly opened substitute."""
        directory = descriptor = probe = None
        try:
            require(self.file is not None and not self.file.closed, 'Live lease required')
            directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory)
            require((info.st_dev, info.st_ino) == self._directory_identity, 'Lease directory changed')
            original = os.fstat(self.file.fileno())
            named = os.stat(self.key + '.lock', dir_fd=directory, follow_symlinks=False)
            require(stat.S_ISREG(named.st_mode) and named.st_uid == os.getuid() and named.st_nlink == 1
                and stat.S_IMODE(named.st_mode) == 0o600
                and (named.st_dev, named.st_ino) == (original.st_dev, original.st_ino), 'Original lease required')
            probe = os.open(self.key + '.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ContractError('Original lease is not locked')
            os.close(probe); probe = None
            self._check_cutover()
            descriptor = os.dup(self.file.fileno())
            yield descriptor, directory, self.key + '.lock'
        finally:
            for selected in (descriptor, probe, directory):
                if selected is not None: os.close(selected)
