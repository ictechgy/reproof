"""Pure validation of an app-reported iOS runtime identity marker."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import uuid

from .core import ContractError, require


RUNTIME_IDENTITY_FILENAME = "runtime-identity.json"
RUNTIME_IDENTITY_KIND = "ios-runtime-identity"
RUNTIME_IDENTITY_GRADE = "app-reported-runtime-id"
MAX_RUNTIME_IDENTITY_BYTES = 4096
_BUNDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)+\Z")
_BUILD_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEYS = frozenset({
    "schemaVersion", "kind", "bundleId", "buildId", "runId", "profileDigest", "startedAtMs",
})


def _uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class IOSSanitationReceipt:
    policy_digest: str
    run_id: str
    stage: str
    started_at_ms: int
    completed_at_ms: int
    path_count: int
    user_defaults_key_count: int
    keychain_item_count: int

    grade = 'app-self-verified-sanitation'

    def public(self):
        return {'schemaVersion':1, 'kind':'ios-app-sanitation-receipt',
            'policyDigest':self.policy_digest, 'runId':self.run_id, 'stage':self.stage,
            'startedAtMs':self.started_at_ms, 'completedAtMs':self.completed_at_ms,
            'pathCount':self.path_count, 'userDefaultsKeyCount':self.user_defaults_key_count,
            'keychainItemCount':self.keychain_item_count, 'status':'complete'}


def _sanitation(value, *, policy_digest, run_id, counts, stage):
    require(type(policy_digest) is str and _DIGEST.fullmatch(policy_digest)
            and stage in ('launch', 'cleanup'), 'Invalid sanitation expectation')
    limits={'pathCount':64, 'userDefaultsKeyCount':128, 'keychainItemCount':32}
    require(type(counts) is dict and set(counts)==set(limits)
            and all(type(counts[key]) is int and 0 <= counts[key] <= maximum
                    for key,maximum in limits.items()), 'Invalid sanitation resource counts')
    keys={'schemaVersion','kind','policyDigest','runId','stage','startedAtMs',
          'completedAtMs','pathCount','userDefaultsKeyCount','keychainItemCount','status'}
    require(type(value) is dict and set(value)==keys
            and type(value['schemaVersion']) is int and value['schemaVersion']==1
            and value['kind']=='ios-app-sanitation-receipt' and value['status']=='complete'
            and value['policyDigest']==policy_digest and value['runId']==run_id and value['stage']==stage
            and all(type(value[key]) is int and value[key]==counts[key] for key in limits)
            and type(value['startedAtMs']) is int and type(value['completedAtMs']) is int
            and 0 <= value['startedAtMs'] <= value['completedAtMs'] <= 2**63-1,
            'Incomplete or mismatched iOS sanitation receipt')
    return IOSSanitationReceipt(policy_digest, run_id, stage, value['startedAtMs'], value['completedAtMs'],
        value['pathCount'],value['userDefaultsKeyCount'],value['keychainItemCount'])


@dataclass(frozen=True, slots=True)
class IOSRuntimeIdentityObservation:
    bundle_id: str
    build_id: str
    run_id: str
    profile_digest: str
    started_at_ms: int
    sanitation: IOSSanitationReceipt | None = None

    grade = RUNTIME_IDENTITY_GRADE

    def public(self):
        return {
            "schemaVersion": 2 if self.sanitation is not None else 1,
            "kind": RUNTIME_IDENTITY_KIND,
            "bundleId": self.bundle_id,
            "buildId": self.build_id,
            "runId": self.run_id,
            "profileDigest": self.profile_digest,
            "startedAtMs": self.started_at_ms,
            "grade": self.grade,
        } | ({'sanitation':self.sanitation.public()} if self.sanitation is not None else {})


def validate_ios_runtime_identity(document, *, bundle_id, build_id, profile_digest, run_id,
                                  min_started_at_ms=None, sanitation_policy_digest=None,
                                  sanitation_counts=None, sanitation_stage='launch'):
    """Validate one bounded marker against the launch and app profile contract.

    The result records only app-reported fields. It intentionally carries no
    installed-artifact hash and grants no signing, device, or qualification authority.
    """
    try:
        has_sanitation=sanitation_policy_digest is not None
        require((sanitation_counts is not None)==has_sanitation
                and sanitation_stage in ('launch','cleanup')
                and (has_sanitation or sanitation_stage=='launch'), 'Invalid sanitation contract')
        require(type(document) is dict and set(document) == _KEYS | ({'sanitation'} if has_sanitation else set()),
                "Invalid iOS runtime identity marker")
        encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        require(0 < len(encoded) <= MAX_RUNTIME_IDENTITY_BYTES,
                "iOS runtime identity marker is oversized")
        require(type(bundle_id) is str and _BUNDLE.fullmatch(bundle_id),
                "Invalid expected iOS runtime bundle")
        require(type(build_id) is str and _BUILD_ID.fullmatch(build_id),
                "Invalid expected iOS runtime build")
        require(type(profile_digest) is str and _DIGEST.fullmatch(profile_digest),
                "Invalid expected iOS runtime profile digest")
        require(_uuid(run_id), "Invalid expected iOS runtime run")
        if min_started_at_ms is None:
            min_started_at_ms = 0
        require(type(min_started_at_ms) is int and not isinstance(min_started_at_ms, bool)
                and 0 <= min_started_at_ms <= 2**63 - 1,
                "Invalid minimum iOS runtime start time")
        require(type(document["schemaVersion"]) is int
                and not isinstance(document["schemaVersion"], bool)
                and document["schemaVersion"] == (2 if has_sanitation else 1)
                and document["kind"] == RUNTIME_IDENTITY_KIND,
                "Invalid iOS runtime identity marker version")
        require(type(document["bundleId"]) is str and _BUNDLE.fullmatch(document["bundleId"])
                and document["bundleId"] == bundle_id,
                "iOS runtime bundle identity differs from its launch")
        require(type(document["buildId"]) is str and _BUILD_ID.fullmatch(document["buildId"])
                and document["buildId"] == build_id,
                "iOS runtime build identity differs from its artifact")
        require(_uuid(document["runId"]) and document["runId"] == run_id,
                "iOS runtime run identity differs from its launch")
        require(type(document["profileDigest"]) is str
                and _DIGEST.fullmatch(document["profileDigest"])
                and document["profileDigest"] == profile_digest,
                "iOS runtime profile identity differs from its launch")
        require(type(document["startedAtMs"]) is int and not isinstance(document["startedAtMs"], bool)
                and 0 <= document["startedAtMs"] <= 2**63 - 1
                and document["startedAtMs"] >= min_started_at_ms,
                "Stale or invalid iOS runtime identity marker")
        sanitation=(_sanitation(document['sanitation'],policy_digest=sanitation_policy_digest,
            run_id=run_id,counts=sanitation_counts,stage=sanitation_stage) if has_sanitation else None)
        return IOSRuntimeIdentityObservation(
            document["bundleId"], document["buildId"], document["runId"],
            document["profileDigest"], document["startedAtMs"], sanitation)
    except (ContractError, TypeError, ValueError, OverflowError):
        raise ContractError("Invalid iOS runtime identity marker") from None


__all__ = [
    "IOSRuntimeIdentityObservation",
    "IOSSanitationReceipt",
    "MAX_RUNTIME_IDENTITY_BYTES",
    "RUNTIME_IDENTITY_FILENAME",
    "RUNTIME_IDENTITY_GRADE",
    "RUNTIME_IDENTITY_KIND",
    "validate_ios_runtime_identity",
]
