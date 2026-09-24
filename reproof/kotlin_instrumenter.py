"""Bounded Kotlin PSI instrumentation for Android activity tap traces.

The public API accepts source text explicitly.  The Kotlin compiler is run in a
short-lived subprocess over a private temporary directory, so compiler output
never receives application source on its logging channel.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Dict, List

from .core import ContractError, require
from .resources import ResourceError, distribution_kind, read_resource, resource_root


_ACTIVITY = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*\Z")
_TARGET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")
_MAX_FILES = 128
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_TOOL = resource_root() / "tools" / "kotlin-instrumenter"


def instrument_kotlin(source_files: dict[str, str], activity: str, tap_targets: list[str]) -> dict:
    """Instrument a configured Android Activity using Kotlin PSI.

    ``source_files`` is the complete, explicit public Kotlin input set.  Only
    the activity source file is returned in ``files``; callers overlay that map
    on their already validated source snapshot.  Any unsupported or ambiguous
    shape raises :class:`ContractError` before a partial output is returned.
    """
    _validate_inputs(source_files, activity, tap_targets)
    result = _run_tool(source_files, activity, ','.join(tap_targets))
    return _validate_result(result, source_files, activity, tap_targets)


def configure_gradle(settings: str, module: str) -> dict[str, str]:
    """Insert a fixed included plugin using Kotlin PSI statement boundaries."""
    sources = {'settings.gradle.kts': settings, 'module.gradle.kts': module}
    require(all(type(raw) is str and len(raw.encode()) <= _MAX_FILE_BYTES for raw in sources.values()),
            'Invalid Kotlin Gradle build input')
    result = _run_tool(sources, '--gradle', 'v1')
    require(type(result) is dict and set(result) == {'schemaVersion', 'files'}
            and type(result['schemaVersion']) is int and result['schemaVersion'] == 1
            and type(result['files']) is dict and set(result['files']) == set(sources)
            and all(type(raw) is str and raw != sources[name] and len(raw.encode()) <= _MAX_FILE_BYTES + 1024
                    for name, raw in result['files'].items()), 'Invalid Gradle integration result')
    return result['files']


def _run_tool(source_files, mode, selection):
    java_home = _java_home()
    with tempfile.TemporaryDirectory(prefix="reproof-kotlin-") as directory:
        root = Path(directory)
        for relative, source in source_files.items():
            destination = root / PurePosixPath(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source, encoding="utf-8", newline="")
            destination.chmod(0o600)

        tool = _TOOL
        if distribution_kind() == 'installed':
            # Compile the fixed analyzer beside this private run. Installed
            # resources stay immutable; no checkout cache is needed or copied.
            tool = root / '_fixed-tool'
            try:
                for name in ('build.sh', 'src/main/java/io/reproof/instrumenter/KotlinInstrumenter.java'):
                    destination = tool / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(read_resource('tools/kotlin-instrumenter/' + name))
            except (ResourceError, OSError):
                raise ContractError('Installed Kotlin analyzer resources are unavailable') from None
        _ensure_built(java_home, tool_root=tool)
        classes = tool / "build" / "classes"
        classpath_file = classes / "classpath"
        try:
            classpath = classpath_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ContractError("Kotlin PSI instrumenter is unavailable") from exc

        command = [
            str(Path(java_home) / "bin" / "java"),
            "-Djava.awt.headless=true",
            "-cp",
            f"{classes}{os.pathsep}{classpath}",
            "io.reproof.instrumenter.KotlinInstrumenter",
            str(root),
            mode,
            selection,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(tool),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
                env=_subprocess_environment(java_home),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContractError("Kotlin PSI instrumenter could not run") from exc
        if completed.returncode != 0:
            # Never surface compiler stderr: it can contain source excerpts.
            raise ContractError("Kotlin source cannot be safely instrumented")
        if len(completed.stdout.encode("utf-8")) > _MAX_TOTAL_BYTES:
            raise ContractError("Kotlin instrumenter output exceeds the input bound")
        try:
            result = json.loads(completed.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ContractError("Kotlin instrumenter returned invalid output") from exc
    return result


def _validate_inputs(source_files: dict[str, str], activity: str, tap_targets: list[str]) -> None:
    require(isinstance(source_files, dict) and 0 < len(source_files) <= _MAX_FILES,
            "Explicit Kotlin source files are required")
    total = 0
    for relative, source in source_files.items():
        require(isinstance(relative, str) and _safe_relative_path(relative) and relative.endswith(".kt"),
                "Unsafe Kotlin source path")
        require(isinstance(source, str), "Kotlin source text is required")
        size = len(source.encode("utf-8"))
        require(size <= _MAX_FILE_BYTES and (total := total + size) <= _MAX_TOTAL_BYTES,
                "Kotlin source exceeds input limit")
    require(isinstance(activity, str) and _ACTIVITY.fullmatch(activity) is not None and len(activity) <= 240,
            "Invalid activity FQCN")
    require(isinstance(tap_targets, list) and 0 < len(tap_targets) <= 64,
            "Tap target list is invalid")
    seen = set()
    for target in tap_targets:
        require(isinstance(target, str) and _TARGET.fullmatch(target) is not None,
                "Tap target is invalid")
        require(target not in seen, "Tap targets must be unique")
        seen.add(target)


def _safe_relative_path(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value or ".." in value:
        return False
    parts = value.split("/")
    return all(part and part != "." and not part.startswith(".") and _PATH_PART.fullmatch(part) for part in parts)


def _java_home() -> str:
    configured = os.environ.get("JAVA_HOME")
    if configured and (Path(configured) / "bin" / "java").is_file():
        return configured
    candidate = "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
    if (Path(candidate) / "bin" / "java").is_file():
        return candidate
    raise ContractError("JDK 17 is required for Kotlin PSI instrumentation")


def _subprocess_environment(java_home: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if key in {'HOME', 'USER', 'LANG', 'LC_ALL', 'PATH'}}
    environment["JAVA_HOME"] = java_home
    return environment


def _ensure_built(java_home: str, *, tool_root=None) -> None:
    tool = _TOOL if tool_root is None else tool_root
    classes = tool / "build" / "classes"
    class_file = classes / "io" / "reproof" / "instrumenter" / "KotlinInstrumenter.class"
    sources = list((tool / "src").rglob("*.java"))
    newest_source = max((path.stat().st_mtime_ns for path in sources), default=0)
    if (classes / "classpath").is_file() and class_file.is_file() and class_file.stat().st_mtime_ns >= newest_source:
        return
    try:
        completed = subprocess.run(
            ['/bin/sh', str(tool / "build.sh")],
            cwd=str(tool),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
            env=_subprocess_environment(java_home),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError("Kotlin PSI instrumenter could not be built") from exc
    if completed.returncode != 0:
        raise ContractError("Kotlin PSI compiler cache is unavailable")


def _validate_result(result: object, source_files: dict[str, str], activity: str,
                     tap_targets: list[str]) -> dict:
    require(isinstance(result, dict) and result.get("schemaVersion") == 1,
            "Kotlin instrumenter returned an unsupported schema")
    path = result.get("activityPath")
    files = result.get("files")
    sites = result.get("sites")
    require(isinstance(path, str) and path in source_files, "Kotlin instrumenter returned an invalid activity path")
    require(isinstance(files, dict) and set(files) == {path}, "Kotlin instrumenter returned an invalid file set")
    require(isinstance(files[path], str) and files[path] != source_files[path],
            "Kotlin instrumenter made no source change")
    require(isinstance(sites, list) and len(sites) == len(tap_targets),
            "Kotlin instrumenter returned incomplete site metadata")
    expected = set(tap_targets)
    seen = set()
    for site in sites:
        require(isinstance(site, dict) and set(site) == {"id", "path", "line", "target", "kind"},
                "Invalid Kotlin tap site metadata")
        require(isinstance(site["id"], str) and re.fullmatch(r"s[0-9a-f]{16}", site["id"]),
                "Invalid Kotlin tap site identifier")
        require(site["path"] == path and type(site["line"]) is int and site["line"] >= 1
                and site["target"] in expected and site["kind"] == "tap",
                "Invalid Kotlin tap site metadata")
        require(site["target"] not in seen, "Duplicate Kotlin tap site metadata")
        seen.add(site["target"])
    require(seen == expected, "Kotlin tap site coverage is incomplete")
    # The source file map is deliberately constrained to the caller's explicit
    # input paths.  The compiler may add only the requested runtime import and
    # hook calls; no generated or ambient files can cross this boundary.
    require(set(files).issubset(set(source_files)), "Kotlin instrumenter returned an unrequested file")
    return result
