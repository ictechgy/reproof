"""Offline builder and pinned loader for the fixed Android signing owner."""
from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import time
import uuid
import zipfile

from .repair_signing_recovery import SigningOwnerTools
from .resources import read_resource


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_NAMES = (
    "native/android-signing-owner/SigningOwner.java",
    "native/android-signing-owner/fd_identity.c",
)
_JAR_NAME = "reproloop-android-signing-owner.jar"
_JNI_NAME = "libreproloop_signing_owner_fd.dylib"
_MANIFEST_NAME = "tools-manifest.json"
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_TOOL_BYTES = 256 * 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_PROCESS_OUTPUT = 128 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_RENAME_EXCL = 0x00000004
_REQUIRED_CLASSES = frozenset({
    "io/reproloop/signing/SigningOwner.class",
    "io/reproloop/signing/SigningOwner$Config.class",
    "io/reproloop/signing/SigningOwner$Rejected.class",
    "io/reproloop/signing/SigningOwner$TextCheck.class",
    "io/reproloop/signing/SigningOwner$ManifestIdentity.class",
    "io/reproloop/signing/SigningOwner$BoundedSink.class",
})


class AndroidSigningToolsError(RuntimeError):
    """A static, non-secret build or load failure."""

    _MESSAGES = {
        "android_signing_tools_configuration":
            "Android signing owner build configuration is invalid",
        "android_signing_tools_tool":
            "A pinned Android signing build tool is unavailable or changed",
        "android_signing_tools_resource":
            "Fixed Android signing owner sources are unavailable or changed",
        "android_signing_tools_output":
            "Use a new Android signing owner output directory",
        "android_signing_tools_cancelled":
            "Android signing owner build was cancelled",
        "android_signing_tools_timeout":
            "Android signing owner build exceeded its deadline",
        "android_signing_tools_process_output":
            "Android signing owner build tool output exceeded its bound",
        "android_signing_tools_process":
            "Android signing owner build tool failed",
        "android_signing_tools_process_unconfirmed":
            "Android signing owner build process termination is unconfirmed",
        "android_signing_tools_manifest":
            "Android signing owner tools manifest is invalid or changed",
    }

    def __init__(self, code="android_signing_tools_configuration", *,
                 cleanup_confirmed=True):
        self.code = code
        self.cleanup_confirmed = cleanup_confirmed is True
        super().__init__(self._MESSAGES.get(
            code, self._MESSAGES["android_signing_tools_configuration"]))


def _require(condition, code):
    if not condition:
        raise AndroidSigningToolsError(code)


def _active(cancellation, deadline_monotonic):
    _require(type(deadline_monotonic) in (int, float)
             and math.isfinite(deadline_monotonic),
             "android_signing_tools_configuration")
    _require(not cancellation.is_set(), "android_signing_tools_cancelled")
    _require(time.monotonic() < deadline_monotonic,
             "android_signing_tools_timeout")


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def _sha_file(path, *, maximum=_MAX_TOOL_BYTES, single_link=False):
    descriptor = None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode)
                 and (not single_link or before.st_nlink == 1)
                 and 0 < before.st_size <= maximum,
                 "android_signing_tools_tool")
        digest = hashlib.sha256()
        total = 0
        while total <= maximum:
            chunk = os.read(
                descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        _require(total == before.st_size <= maximum
                 and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (before.st_size, before.st_mtime_ns, before.st_ctime_ns),
                 "android_signing_tools_tool")
        return digest.hexdigest(), total
    except AndroidSigningToolsError:
        raise
    except OSError:
        raise AndroidSigningToolsError(
            "android_signing_tools_tool") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _sha_at(directory, name, *, maximum):
    descriptor = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=directory)
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid == os.getuid()
                 and not (stat.S_IMODE(before.st_mode) & 0o022)
                 and 0 < before.st_size <= maximum,
                 "android_signing_tools_manifest")
        digest = hashlib.sha256()
        total = 0
        while total <= maximum:
            chunk = os.read(
                descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns)
        _require(total == before.st_size <= maximum
                 and (after.st_dev, after.st_ino, after.st_size,
                      after.st_mtime_ns, after.st_ctime_ns) == identity,
                 "android_signing_tools_manifest")
        return digest.hexdigest(), total, identity
    except AndroidSigningToolsError:
        raise
    except OSError:
        raise AndroidSigningToolsError(
            "android_signing_tools_manifest") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resolved_file(path, name, digest, *, executable):
    selected = Path(path)
    _require(selected.is_absolute() and type(digest) is str
             and _DIGEST.fullmatch(digest) is not None,
             "android_signing_tools_configuration")
    try:
        selected = selected.resolve(strict=True)
        info = selected.lstat()
    except OSError:
        raise AndroidSigningToolsError("android_signing_tools_tool") from None
    _require(selected.name == name and stat.S_ISREG(info.st_mode)
             and not selected.is_symlink()
             and (not executable or bool(info.st_mode & 0o111)),
             "android_signing_tools_tool")
    actual, _ = _sha_file(selected)
    _require(actual == digest, "android_signing_tools_tool")
    return selected


@dataclass(frozen=True, slots=True)
class AndroidSigningBuildTools:
    jdk_home: Path
    java: Path
    java_sha256: str
    javac: Path
    javac_sha256: str
    jar: Path
    jar_sha256: str
    clang: Path
    clang_sha256: str
    apksigner_jar: Path
    apksigner_jar_sha256: str
    _jni_headers: tuple[tuple[Path, str, int], ...] = field(
        init=False, repr=False)

    def __post_init__(self):
        home = Path(self.jdk_home)
        _require(home.is_absolute(), "android_signing_tools_configuration")
        try:
            home = home.resolve(strict=True)
            info = home.lstat()
        except OSError:
            raise AndroidSigningToolsError("android_signing_tools_tool") from None
        _require(stat.S_ISDIR(info.st_mode) and not home.is_symlink(),
                 "android_signing_tools_tool")
        object.__setattr__(self, "jdk_home", home)
        for attribute, basename, digest_attribute, executable in (
            ("java", "java", "java_sha256", True),
            ("javac", "javac", "javac_sha256", True),
            ("jar", "jar", "jar_sha256", True),
            ("clang", "clang", "clang_sha256", True),
            ("apksigner_jar", "apksigner.jar", "apksigner_jar_sha256", False),
        ):
            selected = _resolved_file(
                getattr(self, attribute), basename,
                getattr(self, digest_attribute), executable=executable)
            object.__setattr__(self, attribute, selected)
        _require(self.java == home / "bin/java"
                 and self.javac == home / "bin/javac"
                 and self.jar == home / "bin/jar",
                 "android_signing_tools_tool")
        headers = []
        for selected in (home / "include/jni.h",
                         home / "include/darwin/jni_md.h"):
            digest, size = _sha_file(selected, maximum=1024 * 1024)
            headers.append((selected, digest, size))
        object.__setattr__(self, "_jni_headers", tuple(headers))

    def verify(self):
        for path, expected in (
            (self.java, self.java_sha256),
            (self.javac, self.javac_sha256),
            (self.jar, self.jar_sha256),
            (self.clang, self.clang_sha256),
            (self.apksigner_jar, self.apksigner_jar_sha256),
        ):
            actual, _ = _sha_file(path)
            _require(actual == expected, "android_signing_tools_tool")
        for path, expected, size in self._jni_headers:
            actual, current_size = _sha_file(path, maximum=1024 * 1024)
            _require((actual, current_size) == (expected, size),
                     "android_signing_tools_tool")

    def manifest_record(self):
        self.verify()
        return {
            "jdkHome": str(self.jdk_home),
            "java": _tool_record(self.java, self.java_sha256),
            "javac": _tool_record(self.javac, self.javac_sha256),
            "jar": _tool_record(self.jar, self.jar_sha256),
            "clang": _tool_record(self.clang, self.clang_sha256),
            "apksignerJar": _tool_record(
                self.apksigner_jar, self.apksigner_jar_sha256),
            "jniHeaders": {
                str(path.relative_to(self.jdk_home)): {
                    "path": str(path), "sha256": digest, "bytes": size}
                for path, digest, size in self._jni_headers
            },
        }


@dataclass(frozen=True, slots=True)
class AndroidSigningOwnerBuild:
    tools: SigningOwnerTools = field(repr=False)
    manifest_digest: str
    output_digest: str


def _tool_record(path, digest):
    actual, size = _sha_file(path)
    _require(actual == digest, "android_signing_tools_tool")
    return {"path": str(path), "sha256": digest, "bytes": size}


def _read_sources():
    selected = {}
    try:
        for name in _SOURCE_NAMES:
            body = read_resource(name)
            _require(type(body) is bytes and 0 < len(body) <= _MAX_SOURCE_BYTES,
                     "android_signing_tools_resource")
            selected[name] = body
    except AndroidSigningToolsError:
        raise
    except Exception:
        raise AndroidSigningToolsError(
            "android_signing_tools_resource") from None
    return selected


def _open_directory(path):
    selected = Path(path)
    _require(selected.is_absolute(), "android_signing_tools_output")
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        for part in selected.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                 and bool(mode & 0o200) and not (mode & 0o022),
                 "android_signing_tools_output")
        return descriptor
    except (OSError, AndroidSigningToolsError):
        if descriptor is not None:
            os.close(descriptor)
        raise AndroidSigningToolsError("android_signing_tools_output") from None


def _write_new(path, body, mode=0o600):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        offset = 0
        while offset < len(body):
            count = os.write(descriptor, body[offset:offset + 1024 * 1024])
            _require(count > 0, "android_signing_tools_output")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_output(path):
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                 "android_signing_tools_output")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path):
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _group_empty(identifier):
    try:
        os.killpg(identifier, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


def _terminate(process):
    cleanup_deadline = time.monotonic() + 1.5
    for selected, window in ((signal.SIGTERM, .4), (signal.SIGKILL, .8)):
        if process.poll() is not None:
            return _group_empty(process.pid)
        try:
            os.killpg(process.pid, selected)
        except ProcessLookupError:
            pass
        boundary = min(cleanup_deadline, time.monotonic() + window)
        while process.poll() is None and time.monotonic() < boundary:
            time.sleep(.01)
        if process.poll() is not None:
            return _group_empty(process.pid)
    if process.poll() is None:
        return False
    return _group_empty(process.pid)


def _drain(streams, buffers):
    if not streams:
        return False
    ready, _, _ = select.select(tuple(streams), (), (), 0)
    overflow = False
    for descriptor in ready:
        try:
            chunk = os.read(descriptor, 16384)
        except BlockingIOError:
            continue
        if not chunk:
            streams.discard(descriptor)
            continue
        buffers[descriptor].extend(chunk)
        overflow = overflow or len(buffers[descriptor]) > _MAX_PROCESS_OUTPUT
    return overflow


def _run_fixed(arguments, work, cancellation, deadline_monotonic):
    _active(cancellation, deadline_monotonic)
    process = None
    streams = set()
    try:
        process = subprocess.Popen(
            tuple(map(str, arguments)), cwd=work, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True,
            env={"LANG": "C", "LC_ALL": "C", "TMPDIR": str(work)})
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            streams.add(stream.fileno())
        buffers = {descriptor: bytearray() for descriptor in streams}
        reason = None
        while process.poll() is None:
            if _drain(streams, buffers):
                reason = "android_signing_tools_process_output"
                break
            if cancellation.is_set():
                reason = "android_signing_tools_cancelled"
                break
            if time.monotonic() >= deadline_monotonic:
                reason = "android_signing_tools_timeout"
                break
            select.select(tuple(streams), (), (), .02)
        if reason is not None:
            confirmed = _terminate(process)
            raise AndroidSigningToolsError(
                reason if confirmed else
                "android_signing_tools_process_unconfirmed",
                cleanup_confirmed=confirmed)
        process.wait()
        while streams and not _drain(streams, buffers):
            if not select.select(tuple(streams), (), (), .02)[0]:
                break
        if not _group_empty(process.pid):
            raise AndroidSigningToolsError(
                "android_signing_tools_process_unconfirmed",
                cleanup_confirmed=False)
        _active(cancellation, deadline_monotonic)
        if any(len(value) > _MAX_PROCESS_OUTPUT for value in buffers.values()):
            raise AndroidSigningToolsError(
                "android_signing_tools_process_output")
        _require(process.returncode == 0, "android_signing_tools_process")
    except AndroidSigningToolsError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        confirmed = process is None or _terminate(process)
        raise AndroidSigningToolsError(
            "android_signing_tools_process" if confirmed else
            "android_signing_tools_process_unconfirmed",
            cleanup_confirmed=confirmed) from None
    except BaseException:
        confirmed = process is None or _terminate(process)
        if confirmed:
            raise
        raise AndroidSigningToolsError(
            "android_signing_tools_process_unconfirmed",
            cleanup_confirmed=False) from None
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass


def _validate_jar(jar_path, classes):
    class_paths = list(classes.rglob("*.class"))
    _require(all(stat.S_ISREG(path.lstat().st_mode)
                 and path.lstat().st_nlink == 1
                 and 0 < path.lstat().st_size <= 1024 * 1024
                 for path in class_paths),
             "android_signing_tools_process")
    expected = {path.relative_to(classes).as_posix()
                for path in class_paths}
    _require(_REQUIRED_CLASSES <= expected and 6 <= len(expected) <= 128
             and all(name.startswith("io/reproloop/signing/SigningOwner")
                     and name.endswith(".class") for name in expected),
             "android_signing_tools_process")
    try:
        with zipfile.ZipFile(jar_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            allowed = expected | {
                "META-INF/", "META-INF/MANIFEST.MF", "io/",
                "io/reproloop/", "io/reproloop/signing/"}
            _require(len(names) == len(set(names)) <= 132
                     and set(names) == allowed
                     and all(not name.startswith("/") and ".." not in
                             Path(name).parts for name in names)
                     and expected <= set(names)
                     and not any(name.endswith((".java", ".c"))
                                 for name in names)
                     and sum(item.file_size for item in infos)
                     <= _MAX_OUTPUT_BYTES,
                     "android_signing_tools_process")
    except (OSError, ValueError, zipfile.BadZipFile):
        raise AndroidSigningToolsError(
            "android_signing_tools_process") from None


def _publish_new(parent_fd, staging, output):
    _require(platform.system() == "Darwin",
             "android_signing_tools_configuration")
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(parent_fd, os.fsencode(staging.name), parent_fd,
                    os.fsencode(output.name), _RENAME_EXCL)
    if result != 0:
        selected = ctypes.get_errno()
        if selected in {errno.EEXIST, errno.ENOTEMPTY}:
            raise AndroidSigningToolsError("android_signing_tools_output")
        raise AndroidSigningToolsError("android_signing_tools_output")
    os.fsync(parent_fd)


def _build_manifest(build_tools, sources, jar_path, library_path,
                    definition_digest):
    source_records = {
        name: {"sha256": hashlib.sha256(body).hexdigest(),
               "bytes": len(body)}
        for name, body in sources.items()
    }
    jar_digest, jar_bytes = _sha_file(
        jar_path, maximum=_MAX_OUTPUT_BYTES, single_link=True)
    library_digest, library_bytes = _sha_file(
        library_path, maximum=_MAX_OUTPUT_BYTES, single_link=True)
    outputs = {
        "ownerJar": {"name": _JAR_NAME, "sha256": jar_digest,
                     "bytes": jar_bytes},
        "jniLibrary": {"name": _JNI_NAME, "sha256": library_digest,
                       "bytes": library_bytes},
    }
    output_digest = hashlib.sha256(_canonical(outputs)).hexdigest()
    manifest = {
        "schemaVersion": 1,
        "kind": "reproloop-android-signing-owner-tools-v1",
        "platform": {"system": platform.system(),
                     "machine": platform.machine()},
        "sources": source_records,
        "buildTools": build_tools.manifest_record(),
        "outputs": outputs,
        "outputDigest": output_digest,
        "ownerDefinitionDigest": definition_digest,
    }
    return manifest, output_digest


def build_android_signing_owner(output, build_tools, *, cancellation,
                                deadline_monotonic):
    _require(type(build_tools) is AndroidSigningBuildTools
             and callable(getattr(cancellation, "is_set", None))
             and type(deadline_monotonic) in (int, float)
             and math.isfinite(deadline_monotonic),
             "android_signing_tools_configuration")
    _active(cancellation, deadline_monotonic)
    selected = Path(output)
    _require(selected.is_absolute() and selected.name not in {"", ".", ".."}
             and not selected.exists() and not selected.is_symlink(),
             "android_signing_tools_output")
    parent = selected.parent
    parent_fd = _open_directory(parent)
    staging = parent / ("." + selected.name + "." + uuid.uuid4().hex)
    staging_created = False
    published = False
    cleanup = True
    try:
        os.mkdir(staging.name, mode=0o700, dir_fd=parent_fd)
        staging_created = True
        work = staging / ".work"
        inputs = work / "inputs"
        classes = work / "classes"
        inputs.mkdir(parents=True, mode=0o700)
        classes.mkdir(mode=0o700)
        sources = _read_sources()
        java_source = inputs / "SigningOwner.java"
        native_source = inputs / "fd_identity.c"
        _write_new(java_source, sources[_SOURCE_NAMES[0]])
        _write_new(native_source, sources[_SOURCE_NAMES[1]])
        owner_jar = staging / _JAR_NAME
        library = staging / _JNI_NAME

        build_tools.verify()
        _run_fixed((
            build_tools.clang, "-dynamiclib", "-O2", "-Wall", "-Wextra",
            "-Werror", "-I", build_tools.jdk_home / "include", "-I",
            build_tools.jdk_home / "include/darwin", native_source,
            "-o", library), work, cancellation, deadline_monotonic)
        build_tools.verify()
        _run_fixed((
            build_tools.javac, "-Xlint:all", "-Werror", "-cp",
            build_tools.apksigner_jar, "-d", classes, java_source),
            work, cancellation, deadline_monotonic)
        build_tools.verify()
        _run_fixed((
            build_tools.jar, "--create", "--file", owner_jar,
            "-C", classes, "."), work, cancellation, deadline_monotonic)
        build_tools.verify()
        _validate_jar(owner_jar, classes)
        jar_digest, _ = _sha_file(
            owner_jar, maximum=_MAX_OUTPUT_BYTES, single_link=True)
        library_digest, _ = _sha_file(
            library, maximum=_MAX_OUTPUT_BYTES, single_link=True)
        staged_tools = SigningOwnerTools(
            build_tools.java, build_tools.java_sha256,
            owner_jar, jar_digest, library, library_digest,
            build_tools.apksigner_jar, build_tools.apksigner_jar_sha256)
        manifest, output_digest = _build_manifest(
            build_tools, sources, owner_jar, library,
            staged_tools.definition_digest)
        body = _canonical(manifest)
        _require(len(body) <= _MAX_MANIFEST_BYTES,
                 "android_signing_tools_manifest")
        _write_new(staging / _MANIFEST_NAME, body)
        shutil.rmtree(work)
        build_tools.verify()
        _require(_read_sources() == sources,
                 "android_signing_tools_resource")
        _require(_sha_file(
            owner_jar, maximum=_MAX_OUTPUT_BYTES, single_link=True) == (
                manifest["outputs"]["ownerJar"]["sha256"],
                manifest["outputs"]["ownerJar"]["bytes"])
            and _sha_file(
                library, maximum=_MAX_OUTPUT_BYTES, single_link=True) == (
                    manifest["outputs"]["jniLibrary"]["sha256"],
                    manifest["outputs"]["jniLibrary"]["bytes"]),
            "android_signing_tools_output")
        _require(set(path.name for path in staging.iterdir()) == {
            _JAR_NAME, _JNI_NAME, _MANIFEST_NAME},
            "android_signing_tools_output")
        for path in (owner_jar, library, staging / _MANIFEST_NAME):
            _sync_output(path)
        _sync_directory(staging)
        _active(cancellation, deadline_monotonic)
        _publish_new(parent_fd, staging, selected)
        published = True
        manifest_digest = hashlib.sha256(body).hexdigest()
        loaded = load_android_signing_owner(selected, manifest_digest)
        return AndroidSigningOwnerBuild(
            loaded, manifest_digest, output_digest)
    except AndroidSigningToolsError as error:
        cleanup = error.cleanup_confirmed
        raise
    except (OSError, ValueError, TypeError, zipfile.BadZipFile):
        raise AndroidSigningToolsError(
            "android_signing_tools_output") from None
    finally:
        os.close(parent_fd)
        if staging_created and not published and cleanup:
            shutil.rmtree(staging, ignore_errors=True)


def _read_manifest(output, expected_digest):
    _require(type(expected_digest) is str
             and _DIGEST.fullmatch(expected_digest) is not None,
             "android_signing_tools_manifest")
    directory = _open_directory(output)
    descriptor = None
    try:
        directory_info = os.fstat(directory)
        _require(directory_info.st_uid == os.getuid()
                 and stat.S_IMODE(directory_info.st_mode) == 0o700,
                 "android_signing_tools_manifest")
        descriptor = os.open(
            _MANIFEST_NAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=directory)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and info.st_uid == os.getuid()
                 and stat.S_IMODE(info.st_mode) == 0o600
                 and 0 < info.st_size <= _MAX_MANIFEST_BYTES,
                 "android_signing_tools_manifest")
        body = bytearray()
        while len(body) <= _MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, min(
                65536, _MAX_MANIFEST_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(descriptor)
        _require(len(body) == info.st_size
                 and (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 == (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                 and hashlib.sha256(body).hexdigest() == expected_digest,
                 "android_signing_tools_manifest")
        value = json.loads(body)
        _require(_canonical(value) == bytes(body),
                 "android_signing_tools_manifest")
        return value, directory
    except AndroidSigningToolsError:
        os.close(directory)
        raise
    except (OSError, ValueError, TypeError):
        os.close(directory)
        raise AndroidSigningToolsError(
            "android_signing_tools_manifest") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _manifest_build_tools(record):
    try:
        _require(type(record) is dict and set(record) == {
            "jdkHome", "java", "javac", "jar", "clang",
            "apksignerJar", "jniHeaders"},
            "android_signing_tools_manifest")
        for name in ("java", "javac", "jar", "clang", "apksignerJar"):
            _require(type(record[name]) is dict and set(record[name]) == {
                "path", "sha256", "bytes"}
                and type(record[name]["bytes"]) is int
                and 0 < record[name]["bytes"] <= _MAX_TOOL_BYTES,
                "android_signing_tools_manifest")
        selected = AndroidSigningBuildTools(
            Path(record["jdkHome"]),
            Path(record["java"]["path"]), record["java"]["sha256"],
            Path(record["javac"]["path"]), record["javac"]["sha256"],
            Path(record["jar"]["path"]), record["jar"]["sha256"],
            Path(record["clang"]["path"]), record["clang"]["sha256"],
            Path(record["apksignerJar"]["path"]),
            record["apksignerJar"]["sha256"])
        _require(selected.manifest_record() == record,
                 "android_signing_tools_manifest")
        return selected
    except AndroidSigningToolsError as error:
        if error.code == "android_signing_tools_manifest":
            raise
        raise AndroidSigningToolsError(
            "android_signing_tools_manifest") from None
    except (KeyError, TypeError, ValueError):
        raise AndroidSigningToolsError(
            "android_signing_tools_manifest") from None


def load_android_signing_owner(output, manifest_digest):
    value, directory = _read_manifest(Path(output), manifest_digest)
    try:
        _require(set(os.listdir(directory)) == {
            _JAR_NAME, _JNI_NAME, _MANIFEST_NAME},
            "android_signing_tools_manifest")
        _require(type(value) is dict and set(value) == {
            "schemaVersion", "kind", "platform", "sources", "buildTools",
            "outputs", "outputDigest", "ownerDefinitionDigest"}
            and value["schemaVersion"] == 1
            and value["kind"] ==
                "reproloop-android-signing-owner-tools-v1"
            and value["platform"] == {
                "system": platform.system(), "machine": platform.machine()}
            and type(value["sources"]) is dict
            and set(value["sources"]) == set(_SOURCE_NAMES)
            and all(type(item) is dict and set(item) == {"sha256", "bytes"}
                    and type(item["sha256"]) is str
                    and _DIGEST.fullmatch(item["sha256"])
                    and type(item["bytes"]) is int
                    and 0 < item["bytes"] <= _MAX_SOURCE_BYTES
                    for item in value["sources"].values()),
                 "android_signing_tools_manifest")
        current_sources = _read_sources()
        _require(value["sources"] == {
            name: {"sha256": hashlib.sha256(body).hexdigest(),
                   "bytes": len(body)}
            for name, body in current_sources.items()},
            "android_signing_tools_manifest")
        build_tools = _manifest_build_tools(value["buildTools"])
        outputs = value["outputs"]
        _require(type(outputs) is dict and set(outputs) == {
            "ownerJar", "jniLibrary"},
            "android_signing_tools_manifest")
        for name, expected_name in (("ownerJar", _JAR_NAME),
                                    ("jniLibrary", _JNI_NAME)):
            record = outputs[name]
            _require(type(record) is dict and set(record) == {
                "name", "sha256", "bytes"}
                and record["name"] == expected_name
                and type(record["sha256"]) is str
                and _DIGEST.fullmatch(record["sha256"])
                and type(record["bytes"]) is int
                and 0 < record["bytes"] <= _MAX_OUTPUT_BYTES,
                "android_signing_tools_manifest")
        _require(type(value["outputDigest"]) is str
                 and _DIGEST.fullmatch(value["outputDigest"])
                 and value["outputDigest"]
                 == hashlib.sha256(_canonical(outputs)).hexdigest()
                 and type(value["ownerDefinitionDigest"]) is str
                 and _DIGEST.fullmatch(value["ownerDefinitionDigest"]),
                 "android_signing_tools_manifest")
        output = Path(output)
        jar_path = output / _JAR_NAME
        library_path = output / _JNI_NAME
        jar_digest, jar_bytes, jar_identity = _sha_at(
            directory, _JAR_NAME, maximum=_MAX_OUTPUT_BYTES)
        library_digest, library_bytes, library_identity = _sha_at(
            directory, _JNI_NAME, maximum=_MAX_OUTPUT_BYTES)
        _require((jar_digest, jar_bytes) == (
            outputs["ownerJar"]["sha256"], outputs["ownerJar"]["bytes"])
            and (library_digest, library_bytes) == (
                outputs["jniLibrary"]["sha256"],
                outputs["jniLibrary"]["bytes"]),
            "android_signing_tools_manifest")
        tools = SigningOwnerTools(
            build_tools.java, build_tools.java_sha256,
            jar_path, jar_digest, library_path, library_digest,
            build_tools.apksigner_jar,
            build_tools.apksigner_jar_sha256)
        _require(tools.definition_digest == value["ownerDefinitionDigest"],
                 "android_signing_tools_manifest")
        _require(_sha_at(directory, _JAR_NAME, maximum=_MAX_OUTPUT_BYTES)
                 == (jar_digest, jar_bytes, jar_identity)
                 and _sha_at(directory, _JNI_NAME, maximum=_MAX_OUTPUT_BYTES)
                 == (library_digest, library_bytes, library_identity),
                 "android_signing_tools_manifest")
        return tools
    except AndroidSigningToolsError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise AndroidSigningToolsError(
            "android_signing_tools_manifest") from None
    finally:
        os.close(directory)


__all__ = [
    "AndroidSigningBuildTools", "AndroidSigningOwnerBuild",
    "AndroidSigningToolsError", "build_android_signing_owner",
    "load_android_signing_owner",
]
