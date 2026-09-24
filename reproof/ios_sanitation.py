"""Fixed, app-owned iOS sanitation policy compiled into debug builds."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import re

from .core import digest, require


_KIND = "ios-app-owned-sanitation"
_ROOTS = frozenset({"documents", "application-support", "caches"})
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAX_POLICY_BYTES = 32 * 1024


@dataclass(frozen=True)
class IOSSanitationPolicy:
    """Canonical immutable view of a validated sanitation policy."""

    _json: str

    @property
    def data(self):
        return json.loads(self._json)

    @property
    def digest(self):
        return digest(self.data)

    @property
    def counts(self):
        document = self.data
        return {
            "pathCount": len(document["paths"]),
            "userDefaultsKeyCount": len(document["userDefaultsKeys"]),
            "keychainItemCount": len(document["keychainGenericPasswords"]),
        }


def _exact_ascii(value, label):
    require(
        type(value) is str
        and 1 <= len(value) <= 256
        and all(0x20 <= ord(character) <= 0x7E for character in value),
        f"Invalid iOS sanitation {label}",
    )
    return value


def _validate_relative_path(value):
    require(
        type(value) is str
        and len(value) <= 512
        and "\\" not in value
        and not value.startswith("/"),
        "Invalid iOS sanitation path",
    )
    components = value.split("/")
    require(
        bool(components)
        and all(_COMPONENT.fullmatch(component) is not None for component in components),
        "Invalid iOS sanitation path",
    )
    return tuple(component.casefold() for component in components)


def validate_ios_sanitation_policy(document):
    """Validate the complete, deliberately narrow app-owned store selection.

    This contract qualifies only the selected main-thread-owned files, standard
    user-default keys, and nonsynchronizable generic-password items. It does not
    claim to detect earlier startup work, background/extension/external writers,
    or open SQLite/CoreData stores.
    """

    keys = {
        "schemaVersion",
        "kind",
        "paths",
        "userDefaultsKeys",
        "keychainGenericPasswords",
    }
    require(
        type(document) is dict
        and set(document) == keys
        and type(document.get("schemaVersion")) is int
        and document["schemaVersion"] == 1
        and document.get("kind") == _KIND,
        "Unsupported iOS sanitation policy",
    )
    paths = document["paths"]
    defaults = document["userDefaultsKeys"]
    keychain = document["keychainGenericPasswords"]
    require(
        type(paths) is list
        and len(paths) <= 64
        and type(defaults) is list
        and len(defaults) <= 128
        and type(keychain) is list
        and len(keychain) <= 32
        and bool(paths or defaults or keychain),
        "Invalid iOS sanitation policy bounds",
    )

    selected_paths = []
    for item in paths:
        require(
            type(item) is dict
            and set(item) == {"root", "relativePath"}
            and type(item.get("root")) is str
            and item.get("root") in _ROOTS,
            "Unsupported iOS sanitation path",
        )
        components = _validate_relative_path(item["relativePath"])
        require(
            not (
                item["root"] == "application-support"
                and components[0] == "reproloop"
            ),
            "The ReproLoop runtime tree is reserved",
        )
        selected_paths.append((item["root"], components))
    for index, (root, components) in enumerate(selected_paths):
        for other_root, other in selected_paths[index + 1 :]:
            if root != other_root:
                continue
            shared = min(len(components), len(other))
            require(
                components[:shared] != other[:shared],
                "Overlapping iOS sanitation paths",
            )

    for value in defaults:
        _exact_ascii(value, "user-default key")
    require(len(set(defaults)) == len(defaults), "Duplicate iOS sanitation user-default key")

    selectors = []
    for item in keychain:
        require(
            type(item) is dict and set(item) == {"service", "account"},
            "Unsupported iOS sanitation keychain selector",
        )
        selector = (
            _exact_ascii(item["service"], "keychain service"),
            _exact_ascii(item["account"], "keychain account"),
        )
        selectors.append(selector)
    require(len(set(selectors)) == len(selectors), "Duplicate iOS sanitation keychain selector")

    try:
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        require(False, "Invalid iOS sanitation policy JSON")
    require(
        len(canonical.encode("utf-8")) <= _MAX_POLICY_BYTES,
        "iOS sanitation policy size limit exceeded",
    )
    return IOSSanitationPolicy(canonical)


def policy_from_app(app):
    """Read and authenticate the fixed sanitation policy embedded in an app."""

    path = Path(app) / "Info.plist"
    require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size <= 4 * 1024 * 1024,
        "Invalid iOS sanitation property list",
    )
    try:
        with path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        require(False, "Invalid iOS sanitation property list")
    require(type(info) is dict, "Invalid iOS sanitation property list")
    has_policy = "ReproSanitationPolicy" in info
    has_digest = "ReproSanitationPolicyDigest" in info
    if not has_policy and not has_digest:
        return None
    require(has_policy and has_digest, "Incomplete embedded iOS sanitation policy")
    policy = validate_ios_sanitation_policy(info["ReproSanitationPolicy"])
    require(
        type(info["ReproSanitationPolicyDigest"]) is str
        and info["ReproSanitationPolicyDigest"] == policy.digest,
        "Embedded iOS sanitation policy digest changed",
    )
    return policy
