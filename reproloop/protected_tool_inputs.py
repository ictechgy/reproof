"""Load fixed VM and signing tool inputs after public/runtime preflight.

These objects contain verified file inputs, never live qualification or a
RunStore reservation. Signing definitions and private material are not read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from . import contracts
from .android_signing_tools import load_android_signing_owner
from .execution.resources import GuestBundle, HostBuildBundle
from .execution.wire import MAX_TRANSFER_BYTES, canonical
from .ios_signing_inputs import IOSSigningOwnerTools
from .ios_signing_tools import load_ios_signing_owner
from .repair_configuration import ProtectedServiceConfiguration
from .repair_signing_recovery import SigningOwnerTools


class ProtectedToolInputsError(RuntimeError):
    def __init__(self):
        self.code = 'protected_tool_inputs'
        super().__init__('Protected VM or signing tool inputs are unavailable or changed')


def _require(value):
    if not value:
        raise ProtectedToolInputsError()


@dataclass(frozen=True, slots=True)
class ProtectedToolProfile:
    profile_id: str
    _profile_document: str = field(repr=False)
    build_bundle: GuestBundle | HostBuildBundle = field(repr=False)
    signing_tools: SigningOwnerTools | IOSSigningOwnerTools = field(repr=False)

    @property
    def profile_document(self):
        return json.loads(self._profile_document)

    def public(self):
        return {'profileId':self.profile_id,
            'profileDigest':contracts.digest(self.profile_document),
            'environmentDigest':self.build_bundle.environment_digest,
            'buildIsolation':self.build_bundle.isolation,
            'signingToolsDigest':self.signing_tools.definition_digest}


@dataclass(frozen=True, slots=True)
class PreparedProtectedToolInputs:
    configuration_digest: str
    _profiles: tuple[ProtectedToolProfile, ...] = field(repr=False)

    def profile(self, profile_id):
        for row in self._profiles:
            if row.profile_id == profile_id:
                return row
        raise ProtectedToolInputsError()

    def public(self):
        return {'schemaVersion':1, 'kind':'protected-tool-inputs', 'executionAuthority':'none',
            'configurationDigest':self.configuration_digest,
            'profiles':[row.public() for row in self._profiles]}

    def verify(self, configuration, issue_configuration, runtime_bundle):
        """Recheck the original declarations, manifests, bytes and service binding."""
        _require(type(configuration) is ProtectedServiceConfiguration
                 and configuration.definition_digest == self.configuration_digest)
        current = load_protected_tool_inputs(configuration, issue_configuration, runtime_bundle)
        _require(current.public() == self.public())


def _load_profile(row):
    build = row['build']
    route = build['route']
    if route['executionClass'] == 'host-build':
        bundle = HostBuildBundle.load(build['hostPath'])
    else:
        bundle = GuestBundle.load(build['bundlePath'])
    recipe = bundle.recipe(route['recipeId'])
    output = 'candidate.apk' if row['platform'] == 'android' else 'candidate.ipa'
    _require(bundle.environment_digest == route['environmentDigest']
        and bundle.metadata['environment']['executionClass'] == route['executionClass']
        and recipe['executionClass'] == route['executionClass']
        and recipe['artifactPolicyId'] == route['artifactPolicyId']
        and recipe['cleanupPolicyId'] == route['cleanupPolicyId']
        and recipe['outputPaths'] == [output]
        and 0 < recipe['maxOutputBytes'] <= MAX_TRANSFER_BYTES
        and bundle.overlay_bytes <= build['journal']['diskBudgetBytes'])
    signing = row['signing']
    if row['platform'] == 'android':
        tools = load_android_signing_owner(signing['toolsPath'], signing['toolsManifestSha256'])
        _require(type(tools) is SigningOwnerTools)
    else:
        tools = load_ios_signing_owner(signing['toolsPath'], signing['toolsManifestSha256'])
        _require(type(tools) is IOSSigningOwnerTools and tools.guardian is not None)
    return ProtectedToolProfile(row['id'], canonical(row).decode('utf-8'), bundle, tools)


def load_protected_tool_inputs(configuration, issue_configuration, runtime_bundle):
    """Read fixed public resources only; create no VM, key or device owner."""
    try:
        _require(type(configuration) is ProtectedServiceConfiguration)
        document = configuration.document
        _require(contracts.digest(document) == configuration.definition_digest)
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        profiles = tuple(_load_profile(row) for row in document['profiles'])
        # File reads can take time. Do not return a result for a stale service,
        # assignment or selected original that changed during those reads.
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        _require(configuration.document == document)
        return PreparedProtectedToolInputs(configuration.definition_digest, profiles)
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        raise ProtectedToolInputsError() from None


__all__ = ['ProtectedToolInputsError', 'ProtectedToolProfile', 'PreparedProtectedToolInputs',
           'load_protected_tool_inputs']
