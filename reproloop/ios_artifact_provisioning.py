"""Private provisioning inputs captured from the same bounded app/IPA parse."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time
import weakref

from . import contracts
from .core import digest, require
from .ios_artifact_transfer import (IosBundleCapability, MAX_APP_ENTRIES, MAX_CODE_OBJECTS,
                                    MAX_EXPANDED_APP_BYTES, _parse_ios_artifact, require_ios_artifact)


_ISSUER = object()
_CAPABILITIES = {}
_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class IosProvisioningBundleInput:
    bundle_path: str
    bundle_id: str
    _cms: bytes | None = field(repr=False)

    @property
    def cms_digest(self):
        return None if self._cms is None else hashlib.sha256(self._cms).hexdigest()

    @property
    def cms_bytes(self):
        return self._cms


@dataclass(frozen=True, slots=True, weakref_slot=True)
class IosProvisionedArtifact:
    artifact: IosBundleCapability
    _bundles: tuple[IosProvisioningBundleInput, ...] = field(repr=False)
    binding_digest: str
    _issuer: object = field(repr=False, compare=False)

    @property
    def bundles(self):
        return self._bundles

    def public(self):
        return {'appDigest': self.artifact.app_digest, 'containerDigest': self.artifact.container_digest,
                'bindingDigest': self.binding_digest, 'bundles': [
                    {'bundlePath': row.bundle_path, 'bundleId': row.bundle_id, 'cmsDigest': row.cms_digest}
                    for row in self._bundles]}


def require_provisioned_ios_artifact(value):
    require(type(value) is IosProvisionedArtifact and value._issuer is _ISSUER,
            'Parser-issued iOS provisioning input required')
    with _LOCK:
        reference = _CAPABILITIES.get(id(value))
        require(reference is not None and reference() is value,
                'Parser-issued iOS provisioning input required')
    require_ios_artifact(value.artifact)
    return value


def parse_provisioned_ios_artifact(source, *, max_bytes=MAX_EXPANDED_APP_BYTES,
                                  max_entries=MAX_APP_ENTRIES, max_code_objects=MAX_CODE_OBJECTS):
    """Capture app and extension profiles privately; no CMS or app code runs."""
    captured = []
    artifact = _parse_ios_artifact(source, max_bytes=max_bytes, max_entries=max_entries,
                                   max_code_objects=max_code_objects, _profile_sink=captured)
    bundles = tuple(sorted((IosProvisioningBundleInput(*row) for row in captured),
                           key=lambda row: (row.bundle_path != '.', row.bundle_path)))
    binding = digest({'appDigest': artifact.app_digest, 'containerDigest': artifact.container_digest,
        'bundles': [{'bundlePath': row.bundle_path, 'bundleId': row.bundle_id, 'cmsDigest': row.cms_digest}
                    for row in bundles]})
    capability = IosProvisionedArtifact(artifact, bundles, binding, _ISSUER)
    identifier = id(capability)
    def forget(reference):
        with _LOCK:
            if _CAPABILITIES.get(identifier) is reference:
                _CAPABILITIES.pop(identifier, None)
    with _LOCK:
        _CAPABILITIES[identifier] = weakref.ref(capability, forget)
    return capability


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedArtifactProvisioning:
    context_digest: str
    artifact_binding_digest: str
    definition_digest: str
    _document: str = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return json.loads(self._document)


class IOSArtifactProvisioningVerifier:
    """Own fixed provisioning requirements for an app and all its extensions.

    This owns its CMS verifier and scoped result lifetimes. It does not sign
    app code, install an app, or issue mobile backend qualification.
    """
    def __init__(self, tools, work_root, *, trust, expected_certificate_sha256,
                 team_id, application_identifier_prefix, selected_device, bundle_policies, _process_owner=None):
        from .ios_artifact_transfer import _check_relative
        from .ios_provisioning_cms import IOSCmsError, IOSCmsTrust, IOSCmsVerifier
        from .ios_provisioning_policy import _policy_identity, _validate_expected_entitlements
        try:
            if type(trust) is not IOSCmsTrust:
                raise ValueError()
            contracts.validate_digest(expected_certificate_sha256)
            require(type(bundle_policies) is dict and '.' in bundle_policies
                    and 1 <= len(bundle_policies) <= MAX_CODE_OBJECTS, 'Invalid bundle provisioning policy')
            checked = {}
            for path, row in bundle_policies.items():
                if path != '.':
                    _check_relative(path)
                require(type(row) is dict and set(row) == {
                    'bundleId', 'profileDigest', 'entitlements', 'entitlementsDigest'},
                    'Invalid bundle provisioning policy')
                _policy_identity(row['bundleId'], team_id, application_identifier_prefix,
                                 selected_device, datetime(1970, 1, 1, tzinfo=timezone.utc))
                contracts.validate_digest(row['profileDigest'])
                contracts.validate_digest(row['entitlementsDigest'])
                entitlements = _validate_expected_entitlements(row['entitlements'], team_id=team_id,
                    application_identifier_prefix=application_identifier_prefix, bundle_id=row['bundleId'])
                require(digest(entitlements) == row['entitlementsDigest'], 'Invalid bundle provisioning policy')
                checked[path] = {**row, 'entitlements': entitlements}
            document = {'trustDigest': trust.definition_digest,
                'certificateSha256': expected_certificate_sha256, 'teamId': team_id,
                'applicationIdentifierPrefix': application_identifier_prefix,
                'selectedDevice': selected_device, 'bundles': checked}
            encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                 allow_nan=False)
            require(len(encoded.encode()) <= 1024 * 1024, 'Bundle provisioning policy is oversized')
        except Exception:
            raise IOSCmsError('cms_configuration') from None
        self._policy_json = encoded
        self.definition_digest = digest(document)
        self._trust = trust
        self._cms = IOSCmsVerifier(tools, work_root, _process_owner=_process_owner)
        self._issuer = object()
        self._issued = {}
        self._closed = False
        self._busy = False
        self._changed = threading.Condition(threading.RLock())

    @property
    def retained_profile_bytes(self):
        return self._cms.retained_profile_bytes

    @property
    def active_processes(self):
        return self._cms.active_processes

    def _release_caps(self, capabilities):
        from .ios_provisioning_cms import IOSCmsError
        for capability in capabilities:
            try:
                self._cms.release_profile(capability)
            except IOSCmsError:
                # Closing either owner already invalidates these exact caps.
                pass

    def _forget(self, identifier, reference):
        with self._changed:
            row = self._issued.get(identifier)
            if row is None or row[0] is not reference:
                return
            self._issued.pop(identifier)
        self._release_caps(row[1])

    def verify(self, artifact, *, context_digest, evaluated_at, cancellation, deadline_monotonic):
        from .ios_provisioning_cms import IOSCmsError, _active
        try:
            require_provisioned_ios_artifact(artifact)
            contracts.validate_digest(context_digest)
            _active(cancellation, deadline_monotonic)
            policy = json.loads(self._policy_json)
            required = policy['bundles']
            require({row.bundle_path for row in artifact.bundles} == set(required)
                    and all(row.bundle_id == required[row.bundle_path]['bundleId']
                            and row.cms_bytes is not None for row in artifact.bundles),
                    'Artifact provisioning inventory differs')
        except IOSCmsError:
            raise
        except Exception:
            raise IOSCmsError('cms_invalid') from None
        with self._changed:
            if self._closed or self._busy or len(self._issued) >= 4096:
                raise IOSCmsError('cms_unavailable')
            self._busy = True
        capabilities = []; results = []; committed = False
        try:
            for row in artifact.bundles:
                _active(cancellation, deadline_monotonic)
                selected = required[row.bundle_path]
                capability = self._cms.verify(row.cms_bytes, expected_cms_digest=row.cms_digest,
                    trust=self._trust, evaluated_at=evaluated_at, cancellation=cancellation,
                    deadline_monotonic=deadline_monotonic)
                capabilities.append(capability)
                assessed = self._cms.assess_profile(capability, trust=self._trust,
                    expected_cms_digest=row.cms_digest, expected_profile_digest=selected['profileDigest'],
                    expected_certificate_sha256=policy['certificateSha256'], bundle_id=row.bundle_id,
                    team_id=policy['teamId'], application_identifier_prefix=policy['applicationIdentifierPrefix'],
                    selected_device=policy['selectedDevice'], expected_entitlements=selected['entitlements'],
                    expected_entitlements_digest=selected['entitlementsDigest'])
                if not assessed.valid:
                    raise IOSCmsError('cms_invalid')
                results.append({'bundlePath': row.bundle_path, 'bundleId': row.bundle_id,
                    'cmsDigest': row.cms_digest, 'contentDigest': capability.content_digest,
                    'profileDigest': assessed.profile_digest,
                    'entitlementsDigest': assessed.entitlements_digest, 'valid': True})
            _active(cancellation, deadline_monotonic)
            document = {'contextDigest': context_digest, 'artifactBindingDigest': artifact.binding_digest,
                'appDigest': artifact.artifact.app_digest, 'containerDigest': artifact.artifact.container_digest,
                'definitionDigest': self.definition_digest, 'evaluatedAt': capabilities[0].evaluated_at.isoformat(),
                'bundles': results}
            with self._changed:
                if self._closed:
                    raise IOSCmsError('cms_cancelled')
                proof = VerifiedArtifactProvisioning(context_digest, artifact.binding_digest, self.definition_digest,
                    json.dumps(document, sort_keys=True, separators=(',', ':')), self._issuer)
                identifier = id(proof)
                owner = weakref.ref(self)
                def forget(reference):
                    selected = owner()
                    if selected is not None:
                        selected._forget(identifier, reference)
                self._issued[identifier] = (weakref.ref(proof, forget), tuple(capabilities))
                committed = True
                return proof
        finally:
            if not committed:
                self._release_caps(capabilities)
            with self._changed:
                self._busy = False
                self._changed.notify_all()

    def require_verified(self, proof, artifact, *, context_digest):
        from .ios_provisioning_cms import IOSCmsError
        try:
            require_provisioned_ios_artifact(artifact)
            with self._changed:
                row = self._issued.get(id(proof))
                require(not self._closed and type(proof) is VerifiedArtifactProvisioning
                        and proof._issuer is self._issuer and row is not None and row[0]() is proof
                        and proof.context_digest == context_digest
                        and proof.artifact_binding_digest == artifact.binding_digest
                        and proof.definition_digest == self.definition_digest,
                        'Untrusted artifact provisioning result')
                capabilities = row[1]
            require(len(capabilities) == len(artifact.bundles), 'Artifact provisioning inventory differs')
            for capability, bundle in zip(capabilities, artifact.bundles):
                self._cms.require_profile(capability, trust=self._trust, expected_cms_digest=bundle.cms_digest)
            return proof.public()
        except Exception:
            raise IOSCmsError('cms_invalid') from None

    def release(self, proof):
        from .ios_provisioning_cms import IOSCmsError
        with self._changed:
            row = self._issued.get(id(proof))
            if type(proof) is not VerifiedArtifactProvisioning or row is None or row[0]() is not proof:
                raise IOSCmsError('cms_invalid')
        self._forget(id(proof), row[0])

    def close(self, *, deadline_monotonic):
        from .ios_provisioning_cms import IOSCmsError
        if type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic):
            raise IOSCmsError('cms_configuration')
        with self._changed:
            self._closed = True
            self._issued.clear()
        collected = self._cms.close(deadline_monotonic=deadline_monotonic)
        with self._changed:
            while self._busy and time.monotonic() < deadline_monotonic:
                self._changed.wait(max(0, deadline_monotonic - time.monotonic()))
            return collected and not self._busy


__all__ = ['IosProvisioningBundleInput', 'IosProvisionedArtifact',
           'parse_provisioned_ios_artifact', 'require_provisioned_ios_artifact',
           'IOSArtifactProvisioningVerifier', 'VerifiedArtifactProvisioning']
