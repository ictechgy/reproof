#!/usr/bin/env python3
"""One-shot, offline Apple Vision diagnostic on two pinned synthetic JPEGs.

Not a native acceptance gate. No app, device, simulator, Live, or network API.
Only the dedicated opened-PR workflow is authorized to run the Apple processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time

DIRECTORY = Path(__file__).resolve().parent
DEVELOPER_DIR = "/Applications/Xcode_16.4.app/Contents/Developer"
OBSERVER_SHA256 = "18ad97694079c779f39111b0f31d0ab6336e5644d61860263c604503c28573c9"
OBSERVER_BYTES = 1819
OBSERVER_TIMEOUT = 15
COMPILE_TIMEOUT = 90
METADATA_TIMEOUT = 10
PIXEL_ORACLE_REVISION = "exact-required-observed-forbidden-v2"
FIXTURES = (
    {
        "name": "03-invalid", "file": "03-invalid-attempt-001.jpg", "bytes": 93904,
        "sha256": "3248da8f3e908bf19bbbbecadeb147f876ce6fe26f36dca38440f5668eb98f50",
        "required": ["CREATE ITEM", "ITEM TOTAL ZERO", "NAME REQUIRED"],
        "forbidden": ["ITEM TOTAL ONE"],
    },
    {
        "name": "04-created", "file": "04-created-attempt-001.jpg", "bytes": 81847,
        "sha256": "ac74d667cc9aa523790993c790dbeadf72f450b012a6d8feb32dbbe0abbeec23",
        "required": ["INVENTORY LIST", "ITEM TOTAL ONE", "BLUE MUG"],
        "forbidden": ["INVENTORY IS EMPTY", "NAME REQUIRED"],
    },
)
STAGES = frozenset({"started", "image_read", "vision_started", "vision_completed",
                    "output_written", "failed", "invalid_arguments"})


class DiagnosticError(Exception):
    """Only fixed, non-private failure codes may cross the report boundary."""


def require(condition, code):
    if not condition:
        raise DiagnosticError(code)


def pinned_bytes(path, size, sha256):
    # Reject symlinks/non-files, bound reads, and bind the actual opened bytes.
    # Even a racing replacement must have the exact approved content hash.
    try:
        require(not any(part.is_symlink() for part in (path, *path.parents)),
                "input_path_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), "input_path_invalid")
            require(info.st_size == size, "input_size_mismatch")
            data = stream.read(size + 1)
        require(len(data) == size, "input_size_mismatch")
        require(hashlib.sha256(data).hexdigest() == sha256, "input_hash_mismatch")
        return data
    except OSError:
        raise DiagnosticError("input_read_failed") from None


def validate_inputs(directory):
    """Validate ALL inputs before ANY subprocess, including host metadata."""
    source = pinned_bytes(directory / "ObserveFrame.swift", OBSERVER_BYTES, OBSERVER_SHA256)
    images = [pinned_bytes(directory / "fixtures" / item["file"], item["bytes"], item["sha256"])
              for item in FIXTURES]
    return source, images


def immutable_copy(path, data):
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o400)


def observer_stages(stderr):
    """Keep only the bounded original allowlisted stage/timing protocol."""
    if isinstance(stderr, bytes):
        stderr = stderr[:16384].decode("ascii", errors="replace")
    if not isinstance(stderr, str):
        return []
    result = []
    for line in stderr[:16384].splitlines():
        match = re.fullmatch(r"reproof_observer stage=([a-z_]+) elapsed_ms=([0-9]{1,9})", line)
        if match and match[1] in STAGES:
            result.append({"stage": match[1], "elapsedMs": int(match[2])})
            if len(result) == 16:
                break
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def parse_observations(stdout):
    require(type(stdout) is bytes and len(stdout) <= 65536, "observer_invalid_output")
    try:
        value = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise DiagnosticError("observer_invalid_output") from None
    require(type(value) is dict and set(value) == {"observations"}
            and type(value["observations"]) is list and len(value["observations"]) <= 128,
            "observer_invalid_output")
    for row in value["observations"]:
        require(type(row) is dict and set(row) == {"text", "confidence"}
                and type(row["text"]) is str and len(row["text"]) <= 512
                and type(row["confidence"]) in (int, float), "observer_invalid_output")
        confidence = row["confidence"]
        # Range check before isfinite also handles arbitrarily large JSON ints.
        require(0 <= confidence <= 1 and math.isfinite(confidence), "observer_invalid_output")
    return value["observations"]


def oracle(observations, fixture):
    normalize = lambda text: " ".join(text.upper().split())
    confident = {normalize(row["text"]) for row in observations if row["confidence"] >= 0.9}
    observed = {normalize(row["text"]) for row in observations}
    missing = [word for word in fixture["required"] if word not in confident]
    forbidden = [word for word in fixture["forbidden"] if word in observed]
    return {"passed": not missing and not forbidden,
            "missingRequired": missing, "observedForbidden": forbidden}


def elapsed_ms(start, end):
    return max(0, int((end - start) * 1000))


def run_process(argv, *, timeout, work, environment):
    # This is the sole process boundary. No shell or environment logging.
    return subprocess.run(argv, cwd=work, env=environment, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def metadata_command(argv, work, environment):
    started = time.monotonic()
    try:
        result = run_process(argv, timeout=METADATA_TIMEOUT, work=work, environment=environment)
    except subprocess.TimeoutExpired:
        raise DiagnosticError("host_metadata_timeout") from None
    except OSError:
        raise DiagnosticError("host_metadata_launch_failed") from None
    require(time.monotonic() < started + METADATA_TIMEOUT, "host_metadata_deadline_exceeded")
    require(result.returncode == 0, "host_metadata_failed")
    require(type(result.stdout) is bytes and len(result.stdout) <= 1024, "host_metadata_invalid")
    try:
        return result.stdout.decode("ascii")
    except UnicodeError:
        raise DiagnosticError("host_metadata_invalid") from None


def verify_host(work, environment):
    require(platform.system() == "Darwin", "github_macos_host_required")
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS",
                "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_REPOSITORY": "ictechgy/reproof",
                "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "1"}
    require(all(os.environ.get(key) == value for key, value in expected.items()),
            "first_github_hosted_pr_run_required")
    require(os.environ.get("DEVELOPER_DIR") == DEVELOPER_DIR and Path(DEVELOPER_DIR).is_dir(),
            "pinned_xcode_unavailable")
    architecture = platform.machine()
    require(architecture in ("arm64", "x86_64"), "host_architecture_invalid")
    version = metadata_command(["/usr/bin/xcodebuild", "-version"], work, environment)
    require(version.strip() == "Xcode 16.4\nBuild version 16F6", "pinned_xcode_version_mismatch")
    system = metadata_command(["/usr/bin/sw_vers"], work, environment)
    match = re.fullmatch(r"ProductName:\s*(macOS|Mac OS X)\nProductVersion:\s*([0-9]{1,2}(?:\.[0-9]{1,2}){1,2})\nBuildVersion:\s*([0-9]{2}[A-Z][0-9]{1,5}[a-z]?)\n?", system)
    require(match is not None, "host_metadata_invalid")
    require(match[2].split(".", 1)[0] == "15", "pinned_macos_version_mismatch")
    return {"status": "verified", "xcodeVersion": "16.4", "xcodeBuild": "16F6",
            "productName": match[1], "productVersion": match[2], "buildVersion": match[3],
            "architecture": architecture}


def compile_observer(source, executable, work, environment):
    result = {"outcome": "started", "timeoutSeconds": COMPILE_TIMEOUT}
    started = time.monotonic()
    try:
        process = run_process(["/usr/bin/xcrun", "swiftc", "-O", "-framework", "Vision",
                               "-framework", "Foundation", str(source), "-o", str(executable)],
                              timeout=COMPILE_TIMEOUT, work=work, environment=environment)
    except subprocess.TimeoutExpired:
        result["outcome"] = "compiler_timeout"
    except OSError:
        result["outcome"] = "compiler_launch_failed"
    else:
        returned = time.monotonic()
        result["elapsedMs"] = elapsed_ms(started, returned)
        if returned >= started + COMPILE_TIMEOUT:
            result["outcome"] = "compiler_deadline_exceeded"
        elif process.returncode != 0:
            result["outcome"] = "compiler_failed"
        elif not executable.is_file() or executable.is_symlink():
            result["outcome"] = "compiler_output_missing"
        else:
            result["outcome"] = "compiled"
    return result


def observe(executable, image, fixture, work, environment):
    attempt = {"name": fixture["name"], "imageFile": fixture["file"],
               "imageSha256": fixture["sha256"], "imageBytes": fixture["bytes"],
               "attempt": 1, "processTimeoutSeconds": OBSERVER_TIMEOUT,
               "observerStages": [], "passed": False}
    started = time.monotonic()
    deadline = started + OBSERVER_TIMEOUT
    try:
        result = run_process([str(executable), str(image)], timeout=OBSERVER_TIMEOUT,
                             work=work, environment=environment)
    except subprocess.TimeoutExpired as error:
        attempt.update(processOutcome="timeout", outcome="observer_timeout",
                       observerStages=observer_stages(error.stderr))
        return attempt
    except OSError:
        attempt.update(processOutcome="launch_failed", outcome="observer_launch_failed")
        return attempt
    returned = time.monotonic()
    attempt["elapsedMs"] = elapsed_ms(started, returned)
    attempt["observerStages"] = observer_stages(result.stderr)
    attempt["processOutcome"] = "returned" if result.returncode == 0 else "failed"
    if returned >= deadline:
        attempt.update(processOutcome="deadline_exceeded", outcome="observer_deadline_exceeded")
        return attempt
    if result.returncode != 0:
        attempt["outcome"] = "observer_failed"
        return attempt
    try:
        observations = parse_observations(result.stdout)
        verdict = oracle(observations, fixture)
    except DiagnosticError as error:
        attempt["outcome"] = str(error)
        # Parsing is inside the same budget, including a failed parse.
        if time.monotonic() >= deadline:
            attempt.update(processOutcome="deadline_exceeded", outcome="observer_deadline_exceeded")
        return attempt
    parsed = time.monotonic()
    attempt.update(observations=observations, oracle=verdict, elapsedMs=elapsed_ms(started, parsed))
    if parsed >= deadline:
        attempt.update(processOutcome="deadline_exceeded", outcome="observer_deadline_exceeded")
    else:
        attempt.update(outcome="passed" if verdict["passed"] else "oracle_mismatch",
                       passed=verdict["passed"])
    return attempt


def new_report():
    return {"schemaVersion": 1, "kind": "offline-vision-diagnostic",
            "nativeAcceptance": False, "diagnosticPassed": False,
            "pixelOracleRevision": PIXEL_ORACLE_REVISION,
            "sourceRunId": "40068410548", "observerSha256": OBSERVER_SHA256,
            "inputValidation": "not_started", "host": {"status": "not_checked"},
            "compiler": {"outcome": "not_started", "timeoutSeconds": COMPILE_TIMEOUT},
            "attempts": [], "observerCalls": 0, "outcome": "not_started",
            "output": {"status": "not_reserved"}}


def execute(report, directory):
    source, images = validate_inputs(directory)
    report["inputValidation"] = "verified"
    with tempfile.TemporaryDirectory(prefix="offline-vision-") as temporary:
        # /var aliases /private/var on macOS. Resolve only our owned temp root.
        work = Path(temporary).resolve(strict=True)
        source_path = work / "ObserveFrame.swift"
        image_paths = [work / fixture["file"] for fixture in FIXTURES]
        immutable_copy(source_path, source)
        for path, data in zip(image_paths, images):
            immutable_copy(path, data)
        # Match the original hosted observer environment and cache locations.
        # Only toolchain selection changes; no environment values are reported.
        environment = os.environ.copy()
        environment["DEVELOPER_DIR"] = DEVELOPER_DIR
        report["host"] = {"status": "checking"}
        report["host"] = verify_host(work, environment)
        executable = work / "observe-frame"
        report["compiler"] = compile_observer(source_path, executable, work, environment)
        if report["compiler"]["outcome"] != "compiled":
            report["outcome"] = report["compiler"]["outcome"]
            return
        # Exactly one fresh process per image. Never retry, even after timeout.
        for path, fixture in zip(image_paths, FIXTURES):
            report["observerCalls"] += 1
            report["attempts"].append(observe(executable, path, fixture, work, environment))
        report["diagnosticPassed"] = all(attempt["passed"] for attempt in report["attempts"])
        report["outcome"] = "completed"


def encode_report(report):
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"


def run(output, *, directory=DIRECTORY, stdout=None):
    """Reserve output first, execute once, always print the sanitized final report.

    A report write/close failure cannot discard completed observations or rerun
    the diagnostic. Existing output paths are never overwritten.
    """
    stdout = sys.stdout if stdout is None else stdout
    report = new_report()
    try:
        destination = Path(output).open("x", encoding="utf-8")
    except OSError:
        report.update(outcome="output_reservation_failed", output={"status": "reservation_failed"})
        stdout.write(encode_report(report))
        return report
    report["output"]["status"] = "reserved"
    try:
        execute(report, Path(directory))
    except DiagnosticError as error:
        report["outcome"] = str(error)
        if report["host"]["status"] == "checking":
            report["host"]["status"] = "failed"
        if report["inputValidation"] != "verified":
            report["inputValidation"] = "failed"
    except OSError:
        # Do not publish exception text (may include private host paths).
        report["outcome"] = "local_io_failed"
    except Exception:
        # Last-resort report boundary: unknown Python/library failures must not
        # expose tracebacks/paths, or cause a retry of any completed observation.
        report["outcome"] = "unexpected_local_failure"
    report["output"]["status"] = "written"
    try:
        destination.write(encode_report(report))
        destination.flush()
    except (OSError, ValueError):
        report["output"]["status"] = "write_failed"
    try:
        destination.close()
    except (OSError, ValueError):
        report["output"]["status"] = ("write_and_close_failed" if report["output"]["status"] == "write_failed"
                                      else "close_failed")
    stdout.write(encode_report(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new report file (exclusive creation)")
    args = parser.parse_args(argv)
    report = run(args.output)
    return 0 if (report["outcome"] == "completed" and report["diagnosticPassed"]
                 and report["output"]["status"] == "written") else 1


if __name__ == "__main__":
    raise SystemExit(main())
