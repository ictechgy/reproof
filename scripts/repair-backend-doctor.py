#!/usr/bin/env python3
"""Read-only prerequisite inspection for the protected repair backend.

This command never boots a guest, provisions resources, reads signing
credentials, touches a device, or creates execution authority.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reproloop.core import ContractError  # noqa: E402
from reproloop.repair import CommandError, run_command  # noqa: E402
from reproloop.execution.protocol import (  # noqa: E402
    validate_environment_descriptor,
    validate_guest_image_manifest,
    validate_signing_policy,
    validate_toolchain_manifest,
)


MAX_DESCRIPTOR_BYTES = 256 * 1024
SENSITIVE_NAMES = {
    "auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519",
    "id_ecdsa", "id_dsa", ".netrc", ".npmrc", ".pypirc",
}
SENSITIVE_SUFFIXES = (".keystore", ".jks", ".p12", ".pfx", ".pem", ".key", ".mobileprovision")


class InspectionError(ValueError):
    pass


def _safe_explicit_path(path: Path) -> None:
    lowered = [part.lower() for part in path.parts]
    if any(name in SENSITIVE_NAMES or name.startswith(".env")
           or name.endswith(SENSITIVE_SUFFIXES) for name in lowered):
        raise InspectionError("Sensitive input paths are not inspected")


def _read_regular(path: Path, maximum: int) -> bytes:
    _safe_explicit_path(path)
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode)) or before.st_size > maximum:
            raise InspectionError("Explicit input is not a bounded regular file")
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("Explicit input is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum
                or metadata.st_dev != before.st_dev or metadata.st_ino != before.st_ino):
            raise InspectionError("Explicit input is not a bounded regular file")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            raise InspectionError("Explicit input is not a bounded regular file")
        return bytes(data)
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InspectionError("Descriptor contains duplicate fields")
        result[key] = value
    return result


def _load_descriptor(path: Path, validator):
    raw = _read_regular(path, MAX_DESCRIPTOR_BYTES)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                InspectionError("Descriptor contains an invalid number")
            ),
        )
        return validator(value)
    except (ContractError, InspectionError, UnicodeError, ValueError, TypeError, RecursionError):
        raise InspectionError("Descriptor validation failed") from None


def _resource_status(path: Path | None, manifest) -> str:
    if path is None:
        return "resource-missing"
    try:
        _safe_explicit_path(path)
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise InspectionError("Resource is not a regular file")
        if metadata.st_size != manifest["sizeBytes"]:
            raise InspectionError("Resource metadata mismatch")
    except (OSError, InspectionError):
        return "invalid"
    # Content verification and usability belong to the G8b provisioning gate.
    return "resource-present-unverified"


def _host_probe() -> tuple[dict, str | None]:
    architecture = platform.machine().lower()
    host = {
        "architecture": architecture if architecture in ("arm64", "x86_64") else "unknown",
        "avFoundation": "unknown",
        "virtualizationFramework": "unknown",
        "virtualizationRuntime": "unknown",
    }
    compiler = shutil.which("swiftc")
    if compiler is None:
        return host, "native-compiler-missing"
    source = """import Foundation
import AVFoundation
import Virtualization
let value: [String: String] = [
  "avFoundation": "available",
  "virtualizationFramework": "available",
  "virtualizationRuntime": VZVirtualMachine.isSupported ? "supported" : "unsupported"
]
let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
"""
    try:
        with tempfile.TemporaryDirectory(prefix="repro-backend-probe-") as temporary:
            directory = Path(temporary)
            source_path = directory / "SupportProbe.swift"
            binary_path = directory / "support-probe"
            module_cache = directory / "module-cache"
            source_path.write_text(source, encoding="utf-8")
            compiler_environment = {key: os.environ[key] for key in ("DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS")
                                    if key in os.environ}
            # Bound output and terminate the owned group, including compiler children.
            try:
                run_command(
                    [compiler, "-module-cache-path", str(module_cache), str(source_path), "-o", str(binary_path)],
                    ROOT, timeout=30, max_output=65536, env_extra=compiler_environment,
                )
            except (CommandError, ContractError):
                return host, "native-probe-compile-failed"
            try:
                output = run_command([str(binary_path)], ROOT, timeout=10, max_output=4096)
            except (CommandError, ContractError):
                return host, "native-probe-run-failed"
            measured = json.loads(output)
            if type(measured) is not dict or set(measured) != {
                "avFoundation", "virtualizationFramework", "virtualizationRuntime"
            }:
                return host, "native-probe-output-invalid"
            if (measured["avFoundation"] != "available"
                    or measured["virtualizationFramework"] != "available"
                    or measured["virtualizationRuntime"] not in ("supported", "unsupported")):
                return host, "native-probe-output-invalid"
            host.update(measured)
            return host, None
    except (OSError, UnicodeError, ValueError):
        return host, "native-probe-failed"


def _inspect(args) -> tuple[dict, int]:
    host, probe_error = _host_probe()
    try:
        free_disk_bytes = shutil.disk_usage(ROOT).free
    except OSError:
        free_disk_bytes = None
    prerequisites = {
        "guestImage": "missing",
        "offlineToolchain": "missing",
        "buildEnvironment": "missing",
        "desktopEnvironment": "optional-missing",
        "mobileEnvironment": "optional-missing",
        "iosSigning": "optional-missing",
    }
    invalid = (probe_error is not None
               or host["virtualizationRuntime"] == "unsupported")
    image = None
    toolchain = None
    environments = {}
    signing = None

    if args.guest_image_manifest is not None:
        try:
            image = _load_descriptor(args.guest_image_manifest, validate_guest_image_manifest)
            prerequisites["guestImage"] = _resource_status(args.guest_image, image)
        except InspectionError:
            prerequisites["guestImage"] = "invalid"
        invalid = invalid or prerequisites["guestImage"] == "invalid"
    elif args.guest_image is not None:
        prerequisites["guestImage"] = "manifest-missing"

    if args.offline_toolchain_manifest is not None:
        try:
            toolchain = _load_descriptor(
                args.offline_toolchain_manifest, validate_toolchain_manifest
            )
            prerequisites["offlineToolchain"] = _resource_status(
                args.offline_toolchain, toolchain
            )
        except InspectionError:
            prerequisites["offlineToolchain"] = "invalid"
        invalid = invalid or prerequisites["offlineToolchain"] == "invalid"
    elif args.offline_toolchain is not None:
        prerequisites["offlineToolchain"] = "manifest-missing"

    for descriptor in args.environment_descriptor:
        try:
            environment = _load_descriptor(descriptor, validate_environment_descriptor)
            execution_class = environment["executionClass"]
            if execution_class in environments:
                raise InspectionError("Duplicate environment class")
            environments[execution_class] = environment
            field = {
                "build-guest": "buildEnvironment",
                "host-build": "hostBuildEnvironment",
                "desktop-guest": "desktopEnvironment",
                "mobile-device": "mobileEnvironment",
            }[execution_class]
            prerequisites[field] = "descriptor-valid"
        except InspectionError:
            invalid = True

    if args.signing_policy_descriptor is not None:
        try:
            signing = _load_descriptor(args.signing_policy_descriptor, validate_signing_policy)
            prerequisites["iosSigning"] = (
                "descriptor-valid" if signing["platform"] == "ios" else "optional-missing"
            )
        except InspectionError:
            prerequisites["iosSigning"] = "invalid"
            invalid = True

    build_environment = environments.get("build-guest")
    if build_environment is not None and image is not None and toolchain is not None:
        linked = (
            build_environment["guestImageId"] == image["id"]
            and build_environment["toolchainId"] == toolchain["id"]
            and build_environment["architecture"] == image["architecture"]
            and build_environment["architecture"] == toolchain["architecture"]
            and build_environment["architecture"] == host["architecture"]
        )
        if not linked:
            prerequisites["buildEnvironment"] = "invalid"
            invalid = True

    mobile_environment = environments.get("mobile-device")
    if mobile_environment is not None and mobile_environment["platform"] == "ios":
        linked = signing is not None and (
            mobile_environment["signingPolicyId"] == signing["id"]
        )
        if not linked:
            prerequisites["mobileEnvironment"] = "invalid"
            invalid = True

    core_present = (
        prerequisites["guestImage"] == "resource-present-unverified"
        and prerequisites["offlineToolchain"] == "resource-present-unverified"
        and prerequisites["buildEnvironment"] == "descriptor-valid"
        and host["avFoundation"] == "available"
        and host["virtualizationFramework"] == "available"
        and host["virtualizationRuntime"] == "supported"
    )
    if invalid:
        status = "backend-unqualified"
    elif core_present:
        status = "prerequisite-inputs-present"
    else:
        status = "prerequisites-missing"

    build_route = "ready-for-g8b-qualification" if core_present else "blocked-prerequisites"
    desktop_route = (
        "ready-for-g8b-qualification"
        if core_present and prerequisites["desktopEnvironment"] == "descriptor-valid"
        else "blocked-prerequisites"
    )
    mobile_route = (
        "ready-for-g8b-qualification"
        if core_present and prerequisites["mobileEnvironment"] == "descriptor-valid"
        else "blocked-prerequisites"
    )
    iphone_route = (
        "ready-for-g8b-qualification"
        if mobile_route == "ready-for-g8b-qualification"
        and prerequisites["iosSigning"] == "descriptor-valid"
        else "blocked-prerequisites"
    )
    report = {
        "schemaVersion": 1,
        "mode": "read-only-inspection",
        "status": status,
        "hostSupport": host,
        "hostResources": {
            "cpuCount": os.cpu_count(),
            "freeDiskBytes": free_disk_bytes,
            "meaning": "observed-prerequisite-only",
        },
        "hostProbe": "current-native-compile-run" if probe_error is None else "failed",
        "prerequisites": prerequisites,
        "routes": {
            "buildGuest": build_route,
            "desktopGuest": desktop_route,
            "mobileDevice": mobile_route,
            "physicalIphoneCandidate": iphone_route,
        },
        "authority": "none",
        "environmentGate": "blocked-unqualified",
        "transportQualification": "blocked-unverified",
    }
    return report, 0 if status == "prerequisite-inputs-present" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect protected backend prerequisites")
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--guest-image-manifest", type=Path)
    parser.add_argument("--guest-image", type=Path)
    parser.add_argument("--offline-toolchain-manifest", type=Path)
    parser.add_argument("--offline-toolchain", type=Path)
    parser.add_argument("--environment-descriptor", type=Path, action="append", default=[])
    parser.add_argument("--signing-policy-descriptor", type=Path)
    args = parser.parse_args(argv)
    report, returncode = _inspect(args)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return returncode


if __name__ == "__main__":
    sys.exit(main())
