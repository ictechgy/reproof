"""Bounded rendering of an operator-owned, Xcode-generated XCTest template."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat

from .core import ContractError, digest, require


MAX_TEMPLATE_BYTES = 1024 * 1024
MAX_RENDERED_BYTES = 128 * 1024
MAX_ENVIRONMENT_FIELDS = 64
MAX_ENVIRONMENT_VALUE_BYTES = 4096
_FORMAT_KEY = "__xctestrun_metadata__"
_TARGET = "ReproLiveTests"
_FIXED_TEST_IDENTIFIERS = frozenset({
    "ReproLiveTests/LiveControlTests/testControlSession",
    "LiveControlTests/testControlSession",
})
_BUNDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_PATH_KEYS = frozenset({
    "TestHostPath", "TestBundlePath", "UITargetAppPath", "DependentProductPaths",
})
_ENVIRONMENT_KEYS = frozenset({
    "REPRO_LIVE_APPLICATION_ID", "REPRO_LIVE_AUTO_PROFILE_DIGEST", "REPRO_LIVE_AUTO_RUN_ID",
    "REPRO_LIVE_CASE", "REPRO_LIVE_GENERAL_ACTIONS", "REPRO_LIVE_GENERAL_PROFILE_DIGEST",
    "REPRO_LIVE_HELPER_INCARNATION", "REPRO_LIVE_HELPER_VERSION", "REPRO_LIVE_HOST_INCARNATION",
    "REPRO_LIVE_LISTEN_HOST", "REPRO_LIVE_LISTEN_PORT", "REPRO_LIVE_PROTOCOL_VERSION",
    "REPRO_LIVE_PROVIDER_INCARNATION", "REPRO_LIVE_RECORD_SDK", "REPRO_LIVE_TOKEN",
    "REPRO_LIVE_URL", "REPRO_TARGET_BUNDLE",
    "REPRO_LIVE_SANITATION_POLICY_DIGEST", "REPRO_LIVE_EGRESS_POLICY_DIGEST",
})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_O_DIRECTORY = os.O_DIRECTORY
_O_NOFOLLOW = os.O_NOFOLLOW
_O_NONBLOCK = os.O_NONBLOCK


def _reject(message="iOS XCTest template rejected"):
    raise ContractError(message)


def _check_relative(value, *, suffix=None):
    require(type(value) is str and value and not value.startswith("/")
            and "\\" not in value and "\x00" not in value,
            "Invalid XCTest template artifact path")
    parts = value.split("/")
    require(all(part and part not in {".", ".."} for part in parts),
            "Invalid XCTest template artifact path")
    require(str(PurePosixPath(value)) == value,
            "Invalid XCTest template artifact path")
    if suffix is not None:
        require(value.casefold().endswith(suffix),
                "Invalid XCTest template artifact path")
    return value


def _placeholder_relative(value, placeholder):
    require(type(value) is str and value.startswith(placeholder + "/"),
            "XCTest template path is not rooted in generated artifacts")
    return _check_relative(value[len(placeholder) + 1:])


def _file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink, info.st_mode & 0o7777)


def _read_owned(path: Path, expected_sha256: str):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path,
            "XCTest template path is not an owned absolute file")
    try:
        info = path.lstat()
    except OSError:
        _reject("XCTest template file is unavailable")
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.getuid()
            and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID
                                    | stat.S_ISGID | stat.S_ISVTX)
            and 0 < info.st_size <= MAX_TEMPLATE_BYTES,
            "XCTest template file boundary rejected")
    signature = _file_signature(info)
    directory = None
    descriptor = None
    try:
        directory = os.open(path.parent, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK)
        descriptor = os.open(path.name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid == os.getuid()
                and not before.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID
                                          | stat.S_ISGID | stat.S_ISVTX)
                and _file_signature(before) == signature,
                "XCTest template file changed while being opened")
        raw = bytearray()
        while True:
            block = os.read(descriptor, min(1024 * 1024, MAX_TEMPLATE_BYTES - len(raw) + 1))
            if not block:
                break
            raw.extend(block)
            require(len(raw) <= MAX_TEMPLATE_BYTES, "XCTest template is oversized")
        after = os.fstat(descriptor)
        require(len(raw) == info.st_size and _file_signature(after) == signature,
                "XCTest template changed while being read")
    except OSError:
        _reject("XCTest template file boundary rejected")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    raw = bytes(raw)
    require(hashlib.sha256(raw).hexdigest() == expected_sha256,
            "XCTest template digest changed")
    return raw, signature


def _target_location(document):
    metadata = document.get(_FORMAT_KEY)
    require(type(metadata) is dict and type(metadata.get("FormatVersion")) is int
            and metadata["FormatVersion"] in {1, 2},
            "Unsupported XCTest template format")
    version = metadata["FormatVersion"]
    if version == 1:
        require(set(document) == {_FORMAT_KEY, _TARGET}
                and type(document[_TARGET]) is dict,
                "Unsupported XCTest template target shape")
        return version, document[_TARGET]
    require(set(document) == {_FORMAT_KEY, "TestConfigurations"}
            and type(document["TestConfigurations"]) is list
            and len(document["TestConfigurations"]) == 1,
            "Unsupported XCTest template configuration shape")
    configuration = document["TestConfigurations"][0]
    require(type(configuration) is dict and type(configuration.get("TestTargets")) is list
            and len(configuration["TestTargets"]) == 1
            and type(configuration["TestTargets"][0]) is dict,
            "Unsupported XCTest template target shape")
    return version, configuration["TestTargets"][0]


def _validate_test_selection(document, target, version):
    containers = [target]
    if version == 2:
        containers.append(document["TestConfigurations"][0])
    for container in containers:
        for key, value in container.items():
            if key in {"OnlyTestIdentifiers", "SkipTestIdentifiers"}:
                require(type(value) is list and all(type(item) is str for item in value),
                        "Invalid XCTest test selection")
                if key == "OnlyTestIdentifiers":
                    require(len(value) == 1 and value[0] in _FIXED_TEST_IDENTIFIERS,
                            "XCTest fixed test selection was changed")
                else:
                    require(value == [],
                            "XCTest fixed test is skipped")
            elif key in {"TestRepetitionCount", "MaximumTestRepetitions"}:
                require(type(value) is int and value == 1,
                        "XCTest fixed test repetition is unsupported")
            elif key in {"RetryOnFailure", "ParallelizationEnabled", "ParallelTestingEnabled",
                         "IsParallelizable"}:
                require(type(value) is bool and value is False,
                        "XCTest fixed test execution policy is unsupported")
            elif key == "TestRepetitionMode":
                require(value == "none", "XCTest fixed test repetition is unsupported")
            elif key == "MaximumConcurrentTestSimulatorDestinations":
                require(type(value) is int and value == 1,
                        "XCTest fixed test parallelism is unsupported")


def _validate_target(document):
    version, target = _target_location(document)
    _validate_test_selection(document, target, version)
    require(target.get("BlueprintName") == _TARGET
            and target.get("IsUITestBundle") is True
            and target.get("IsXCTRunnerHostedTestBundle") is True,
            "XCTest template is not the fixed UI runner")
    host = target.get("TestHostPath")
    app = target.get("UITargetAppPath")
    bundle = target.get("TestBundlePath")
    require(type(host) is str and type(app) is str and type(bundle) is str,
            "XCTest template artifact paths are invalid")
    host_relative = _placeholder_relative(host, "__TESTROOT__")
    app_relative = _placeholder_relative(app, "__TESTROOT__")
    require(host_relative.casefold().endswith(".app")
            and app_relative.casefold().endswith(".app"),
            "XCTest template application path is invalid")
    if bundle.startswith("__TESTHOST__/"):
        bundle_relative = _check_relative(bundle[len("__TESTHOST__/"):], suffix=".xctest")
        expanded_bundle = host + "/" + bundle_relative
    elif bundle.startswith("__TESTROOT__/"):
        expanded_relative = _check_relative(bundle[len("__TESTROOT__/"):], suffix=".xctest")
        require(expanded_relative.startswith(host_relative + "/"),
                "XCTest template bundle is outside its runner")
        bundle_relative = expanded_relative[len(host_relative) + 1:]
        expanded_bundle = bundle
    else:
        _reject("XCTest template test bundle is not rooted in the runner")
    require(bundle_relative == "PlugIns/ReproLiveTests.xctest",
            "XCTest template test bundle is not the fixed runner bundle")
    runner_bundle_identifier = target.get("TestHostBundleIdentifier")
    require(type(runner_bundle_identifier) is str
            and _BUNDLE.fullmatch(runner_bundle_identifier) is not None,
            "XCTest template runner bundle identifier is invalid")
    require(host != app, "XCTest template host and target app must differ")
    require(type(target.get("DependentProductPaths")) is list
            and 1 <= len(target["DependentProductPaths"]) <= 16
            and all(type(value) is str for value in target["DependentProductPaths"]),
            "XCTest template dependent products are invalid")
    expected_products = {host, app, expanded_bundle}
    require(set(target["DependentProductPaths"]) == expected_products,
            "XCTest template dependent products do not match its runner")
    for key in _PATH_KEYS:
        if key not in target:
            continue
        value = target[key]
        if key == "DependentProductPaths":
            continue
        require(type(value) is str, "XCTest template path field is invalid")
    for key in ("EnvironmentVariables", "TestingEnvironmentVariables",
                "UITargetAppEnvironmentVariables"):
        if key in target:
            value = target[key]
            require(type(value) is dict and all(type(k) is str and type(v) is str
                    for k, v in value.items()),
                    "XCTest template environment shape is invalid")
    return (version, target, host, bundle, app, expanded_bundle, host_relative,
            bundle_relative, app_relative, runner_bundle_identifier)


def _replace(value, replacements):
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def _contains(value, needle):
    if isinstance(value, str):
        return needle in value
    if isinstance(value, list):
        return any(_contains(item, needle) for item in value)
    if isinstance(value, dict):
        return any(_contains(item, needle) for item in value.values())
    return False


def _validate_environment(environment):
    require(type(environment) is dict and len(environment) <= MAX_ENVIRONMENT_FIELDS,
            "Invalid XCTest environment")
    for key, value in environment.items():
        require(type(key) is str and key in _ENVIRONMENT_KEYS
                and type(value) is str and "\x00" not in value
                and len(value.encode("utf-8")) <= MAX_ENVIRONMENT_VALUE_BYTES,
                "Unsupported XCTest environment field")
    return environment


@dataclass(frozen=True, slots=True)
class IOSXCTestTemplate:
    path: Path = field(repr=False)
    sha256: str
    _signature: tuple = field(init=False, repr=False, compare=False)
    _document: dict = field(init=False, repr=False, compare=False)
    _paths: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        try:
            path = Path(self.path)
            require(_DIGEST.fullmatch(self.sha256) is not None,
                    "Invalid XCTest template digest")
            raw, signature = _read_owned(path, self.sha256)
            document = plistlib.loads(raw)
            require(type(document) is dict, "Invalid XCTest template plist")
            paths = _validate_target(document)
            object.__setattr__(self, "path", path)
            object.__setattr__(self, "_signature", signature)
            object.__setattr__(self, "_document", deepcopy(document))
            object.__setattr__(self, "_paths", paths[2:])
        except (ContractError, OSError, RuntimeError, ValueError, TypeError,
                plistlib.InvalidFileException):
            raise ContractError("Invalid XCTest template") from None

    @property
    def definition_digest(self):
        return digest({"kind": "ios-xctest-template-v1", "path": str(self.path),
                       "sha256": self.sha256})

    @property
    def format_version(self):
        return self._document[_FORMAT_KEY]["FormatVersion"]

    @property
    def runner_bundle_identifier(self):
        _version, target = _target_location(self._document)
        return target["TestHostBundleIdentifier"]

    def verify(self):
        try:
            raw, signature = _read_owned(self.path, self.sha256)
            require(signature == self._signature,
                    "XCTest template changed since construction")
            document = plistlib.loads(raw)
            require(type(document) is dict, "Invalid XCTest template plist")
            _validate_target(document)
        except (ContractError, OSError, RuntimeError, ValueError, TypeError,
                plistlib.InvalidFileException):
            raise ContractError("Invalid XCTest template") from None

    def render(self, app_paths: dict[str, Path], environment: dict[str, str]):
        try:
            require(type(app_paths) is dict and set(app_paths) == {"helper-host", "helper-runner"},
                    "Invalid XCTest helper app paths")
            selected = {}
            for role, value in app_paths.items():
                path = Path(value)
                require(path.is_absolute() and path.resolve(strict=True) == path
                        and path.is_dir() and path.name.casefold().endswith(".app"),
                        "Invalid XCTest helper app path")
                selected[role] = path
            require(selected["helper-host"] != selected["helper-runner"],
                    "XCTest helper app paths must differ")
            _validate_environment(environment)
            raw, signature = _read_owned(self.path, self.sha256)
            require(signature == self._signature,
                    "XCTest template changed while rendering")
            document = plistlib.loads(raw)
            (_version, target, host, bundle, app, expanded_bundle, _host_relative,
             bundle_relative, _app_relative, _runner_bundle_identifier) = _validate_target(document)
            runner = selected["helper-runner"]
            host_app = selected["helper-host"]
            replacements = sorted((
                (expanded_bundle, str(runner / bundle_relative)),
                (bundle, str(runner / bundle_relative)),
                (host, str(runner)),
                (app, str(host_app)),
            ), key=lambda row: len(row[0]), reverse=True)
            rendered = _replace(deepcopy(document), replacements)
            _version, rendered_target = _target_location(rendered)
            require(rendered_target["TestHostPath"] == str(runner)
                    and rendered_target["TestBundlePath"] == str(runner / bundle_relative)
                    and rendered_target["UITargetAppPath"] == str(host_app)
                    and set(rendered_target["DependentProductPaths"]) == {
                        str(runner), str(host_app), str(runner / bundle_relative)},
                    "XCTest template paths were not relocated exactly")
            require(not _contains(rendered, host)
                    and not _contains(rendered, bundle)
                    and not _contains(rendered, app),
                    "XCTest template retained an original artifact path")
            env = rendered_target.setdefault("EnvironmentVariables", {})
            require(type(env) is dict and all(type(k) is str and type(v) is str
                    for k, v in env.items()), "XCTest template environment shape is invalid")
            env.update(environment)
            output = plistlib.dumps(rendered, fmt=plistlib.FMT_BINARY)
            require(0 < len(output) <= MAX_RENDERED_BYTES,
                    "Rendered XCTest template is oversized")
            return output
        except (ContractError, OSError, RuntimeError, ValueError, TypeError,
                plistlib.InvalidFileException):
            raise ContractError("Invalid XCTest template render") from None


__all__ = ["IOSXCTestTemplate", "MAX_TEMPLATE_BYTES", "MAX_RENDERED_BYTES"]
