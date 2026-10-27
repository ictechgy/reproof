"""Operator-selected Android Debug app-owned preparation, never a replay proof.

Only the shared-authority prepared-session composition consumes this selection.
No command, path, URL, callback import, oracle, or edit instruction is accepted.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import base64
import hashlib
import json
import re
from .core import require

MAX_BYTES = 4096
# Public AOSP test certificate; never an operator-private helper principal.
_PUBLIC_TEST_SIGNER = "a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc"
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = {'schemaVersion', 'kind', 'referenceId', 'profileDigest', 'artifactDigest',
           'fixtureId', 'fixtureVersion', 'policyDigest', 'helperSignerDigest',
           'launchConfigurationDigest'}


@dataclass(frozen=True, slots=True)
class ApprovedAndroidFixture:
    _canonical: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self._canonical).hexdigest()

    @property
    def data(self):
        return json.loads(self._canonical)

    def native(self):
        return {'configurationBase64': base64.b64encode(self._canonical).decode('ascii'),
                'configurationDigest': self.digest}


def validate_android_fixture(value, profile, launch_config=None):
    from .android_profile import AndroidRuntimeProfile
    from .android_launch import require_android_launch
    require(type(profile) is AndroidRuntimeProfile, 'Android fixture requires a runtime profile')
    require(type(value) is dict and set(value) == _FIELDS,
            'Invalid approved Android fixture configuration')
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['kind'] == 'reproof-approved-android-fixture',
            'Unsupported Android fixture configuration')
    for key in ('referenceId', 'fixtureId'):
        require(type(value[key]) is str and _ID.fullmatch(value[key]),
                'Invalid Android fixture identifier')
    for key in ('profileDigest', 'artifactDigest', 'policyDigest', 'helperSignerDigest'):
        require(type(value[key]) is str and _DIGEST.fullmatch(value[key])
                and value[key] != '0' * 64, 'Invalid Android fixture binding digest')
    require(value['helperSignerDigest'] != _PUBLIC_TEST_SIGNER,
            'Public test signing identity cannot authorize fixture preparation')
    require(value['referenceId'] in profile.data['approvedReferences']['preparations']
            and value['profileDigest'] == profile.digest
            and value['artifactDigest'] == profile.data['artifact']['sha256'],
            'Approved Android fixture binding differs')
    require(type(value['fixtureVersion']) is int and 1 <= value['fixtureVersion'] <= 1000,
            'Invalid Android fixture version')
    selected_launch = (None if launch_config is None else
                       require_android_launch(launch_config, profile).digest)
    require(value['launchConfigurationDigest'] == selected_launch,
            'Approved Android fixture launch binding differs')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                     allow_nan=False).encode('ascii')
    require(len(raw) <= MAX_BYTES, 'Android fixture configuration exceeds limit')
    return ApprovedAndroidFixture(raw)


def require_android_fixture(selection, profile, launch_config=None):
    require(type(selection) is ApprovedAndroidFixture, 'Trusted Android fixture selection required')
    checked = validate_android_fixture(selection.data, profile, launch_config)
    require(checked.digest == selection.digest, 'Approved Android fixture selection changed')
    return checked


def android_fixture_endpoint_for_policy(*, fixture_id, fixture_version, policy_digest):
    """Pure G0 endpoint naming before project/profile construction; grants no authority."""
    require(type(fixture_id) is str and _ID.fullmatch(fixture_id)
            and type(fixture_version) is int and 1 <= fixture_version <= 1000
            and type(policy_digest) is str and _DIGEST.fullmatch(policy_digest)
            and policy_digest != '0' * 64, 'Invalid Android fixture semantic policy')
    semantic = {'fixtureId': fixture_id, 'fixtureVersion': fixture_version,
                'policyDigest': policy_digest}
    raw = json.dumps(semantic, sort_keys=True, separators=(',', ':')).encode('ascii')
    return 'f' + hashlib.sha256(raw).hexdigest()[:63]
