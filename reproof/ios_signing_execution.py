"""Fixed native signing and independent inspection within one iOS journal."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import time
import zipfile

from . import contracts
from .execution.artifacts import BlobSet
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_artifact_staging import _directory_at
from .ios_artifact_transfer import (MAX_APP_ENTRIES, MAX_EXPANDED_APP_BYTES, _open_relative_file,
    _scan_tree, parse_ios_artifact, _file_stat_signature)
from .ios_artifact_provisioning import IOSArtifactProvisioningVerifier, parse_provisioned_ios_artifact
from .ios_code_signature import IOSCodeSignatureInspector, IOSCodeSignatureTools, _architectures, _remove_owned_contents
from .ios_native_process import _IOSNativeSession
from .ios_signing_inputs import IOSSigningMaterialResolver, IOSSigningProvisioning
from .repair_signing import SigningObservation, SignatureObservation, SigningFailureObservation
from .repair_signing_recovery import (_context_common, _open_child_directory, _open_owned_regular,
    _replace_at, _validate_context, SigningRecoveryError)


def _require(value, code='ios_signing_execution'):
    if not value: raise SigningRecoveryError(code)


class _Cancellation:
    def __init__(self, caller, operations, operation):
        self.caller, self.operations, self.operation = caller, operations, operation

    def is_set(self):
        return (self.caller.is_set() or self.operations._closed or not self.operation._active
                or self.operation.run.cancelled())


def _active(stop, deadline):
    _require(not stop.is_set(), 'cancelled')
    _require(time.monotonic() < deadline, 'signing_timeout')


@contextmanager
def _execution(operations, operation, context, provisioning, policy, cancellation, deadline, role):
    _validate_context(context)
    _require(operations._native_enabled and type(provisioning) is IOSSigningProvisioning
        and callable(getattr(cancellation, 'is_set', None)) and type(deadline) in (int, float)
        and time.monotonic() < deadline <= time.monotonic()+900)
    checked = operations.definition.validate_policy(policy)
    _require(contracts.digest(checked) == context.signing_policy_digest
        and _context_common(context) == _context_common(operation.context))
    stop = _Cancellation(cancellation, operations, operation)
    operations._require_operation(operation); _active(stop, deadline)
    callback = object()
    with operations._changed:
        _require(not operations._closed); operations._callbacks.add(callback)
    try:
        with operations._operation_directory(operation.operation_id) as descriptor:
            intent, _ = operations._records(descriptor, operation.operation_id, operation.request_digest)
            with operations._producer(descriptor, intent) as handles:
                session = _IOSNativeSession(operations, operation, descriptor, handles, context,
                    provisioning.definition_digest, role)
                yield descriptor, intent, session, stop
    finally:
        with operations._changed:
            operations._callbacks.discard(callback); operations._changed.notify_all()


def _clear_transfer(descriptor, intent):
    transfer = _open_child_directory(descriptor, 'transfer', expected=intent['transferIdentity'])
    try: _remove_owned_contents(transfer, [MAX_APP_ENTRIES+1024]); os.fsync(transfer)
    finally: os.close(transfer)


def _inject_profiles(operations, descriptor, intent, state):
    source = parse_ios_artifact(operations.operation_root(intent['operationId'])/'App.app')
    _require(source.app_digest == state['appDigest'], 'artifact_invalid')
    app = _open_child_directory(descriptor, 'App.app', expected=state['appIdentity'])
    try:
        for path in operations.definition.profile_paths:
            parent = _directory_at(app, '' if path == '.' else path)
            output = None
            try:
                output = os.open('embedded.mobileprovision', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
                info = os.fstat(output)
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)
                os.fchmod(output, 0o600)
                from .ios_signing_operation import _write_bytes
                _write_bytes(output, operations.definition.profile_bytes(path)); os.fsync(parent)
            finally:
                if output is not None: os.close(output)
                os.close(parent)
    finally: os.close(app)
    artifact = parse_ios_artifact(operations.operation_root(intent['operationId'])/'App.app')
    for key in ('applicationId', 'bundleVersion', 'bundleBuild', 'directories', 'files', 'symlinks', 'codeObjects'):
        _require(artifact.manifest[key] == source.manifest[key], 'artifact_invalid')
    state['appDigest'] = artifact.app_digest
    _replace_at(descriptor, 'state.json', state)
    return artifact


def _verify_profiles(operations, session, provisioning, context, stop, deadline, expected_app_digest):
    _active(stop, deadline)
    artifact = parse_provisioned_ios_artifact(session.root/'App.app')
    _require(artifact.artifact.app_digest == expected_app_digest, 'artifact_invalid')
    _require({item.bundle_path for item in artifact.bundles} == set(operations.definition.profile_paths))
    for item in artifact.bundles:
        _require(item.cms_bytes == operations.definition.profile_bytes(item.bundle_path))
    verifier = IOSArtifactProvisioningVerifier(provisioning.tools, session.root/'checks'/'profiles',
        trust=provisioning.trust, expected_certificate_sha256=operations.definition.identity.certificate_sha256,
        team_id=operations.definition.identity.team_id,
        application_identifier_prefix=provisioning.application_identifier_prefix,
        selected_device=provisioning.selected_device, bundle_policies=operations.definition.provisioning_policies,
        _process_owner=session.facade())
    try:
        proof = verifier.verify(artifact, context_digest=context.digest, evaluated_at=datetime.now(timezone.utc),
            cancellation=stop, deadline_monotonic=deadline)
        report = verifier.require_verified(proof, artifact, context_digest=context.digest)
        return contracts.digest(report)
    finally:
        _require(verifier.close(deadline_monotonic=time.monotonic()+5), 'ios_signing_quarantined')


def _check_native_capacity(operations, artifact, input_bytes):
    """Conservative retained-byte bound for this fixed signing policy and tree.

    Account for CMS certificate duplication, XML/DER entitlements, page hashes
    and resource records, including nested inventories. Refuse before key use
    when the bound cannot fit the app/parser or the reserved operation capacity.
    """
    tree = _scan_tree(artifact._source, max_bytes=MAX_EXPANDED_APP_BYTES, max_entries=MAX_APP_ENTRIES)
    chain_bytes = sum(len(body) for body in operations.definition.identity.certificate_chain)
    policy = operations.definition.bundle_policies
    growth = 0
    paths = [item.path for item in tree.files] + list(tree.directories)
    for obj in artifact.manifest['codeObjects']:
        relative = obj['bundlePath']; prefix = '' if relative == '.' else relative+'/'
        info_path = prefix+'Info.plist'
        info_entry = next(item for item in tree.files if item.path == info_path)
        info_fd = _open_relative_file(tree.root, info_path, expected_signature=info_entry.stat_signature)
        try:
            _require(info_entry.size <= 2*1024*1024, 'artifact_invalid')
            info = plistlib.loads(os.read(info_fd, info_entry.size+1))
            _require('CFBundleResourceSpecification' not in info, 'artifact_invalid')
        finally: os.close(info_fd)
        count = len(_architectures(tree, obj['executablePath']))
        executable = next(item for item in tree.files if item.path == obj['executablePath'])
        entitlements = len(plistlib.dumps(policy[relative]['entitlements']))
        growth += count*(2*chain_bytes + 4*entitlements + 65536 + ((executable.size//512)+1)*64)
        growth += 128*1024
        growth += sum(1024 + 16*len(path.encode()) for path in paths if path.startswith(prefix))
        growth += sum(8*chain_bytes + 8*len(row['bundleId'].encode()) + 16384
                      for path, row in policy.items() if path != relative and path.startswith(prefix))
    upper = tree.bytes+growth
    _require(upper <= MAX_EXPANDED_APP_BYTES, 'artifact_invalid')
    required = max(2*upper + input_bytes + 4*1024*1024,
                   upper + input_bytes + MAX_TRANSFER_BYTES + 4*1024*1024)
    _require(required <= operations.minimum_operation_bytes, 'artifact_invalid')
    return upper


class _BoundedArchive:
    def __init__(self, stream): self.stream = stream
    def tell(self): return self.stream.tell()
    def seek(self, *args): return self.stream.seek(*args)
    def flush(self): return self.stream.flush()
    def write(self, body):
        _require(self.stream.tell()+len(body) <= MAX_TRANSFER_BYTES, 'artifact_invalid')
        return self.stream.write(body)


def _export_app(operations, descriptor, intent, artifact):
    tree = _scan_tree(artifact._source, max_bytes=MAX_EXPANDED_APP_BYTES, max_entries=MAX_APP_ENTRIES)
    _require(not tree.symlinks, 'artifact_invalid')
    output = _open_owned_regular(descriptor, 'signed.ipa', expected=intent['fileIdentities']['signed.ipa'], writable=True)
    try:
        os.ftruncate(output, 0); os.lseek(output, 0, os.SEEK_SET)
        with os.fdopen(os.dup(output), 'w+b') as stream:
            with zipfile.ZipFile(_BoundedArchive(stream), 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for path in tree.directories:
                    info = zipfile.ZipInfo('Payload/App.app/'+path+'/', date_time=(1980,1,1,0,0,0))
                    info.create_system = 3; info.external_attr = (stat.S_IFDIR | 0o700) << 16
                    archive.writestr(info, b'')
                for item in tree.files:
                    info = zipfile.ZipInfo('Payload/App.app/'+item.path, date_time=(1980,1,1,0,0,0))
                    info.create_system = 3; info.external_attr = (stat.S_IFREG | (0o700 if item.executable else 0o600)) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    incoming = _open_relative_file(tree.root, item.path, expected_signature=item.stat_signature)
                    digest = hashlib.sha256(); total = 0
                    try:
                        with archive.open(info, 'w', force_zip64=True) as target:
                            while total < item.size:
                                body = os.read(incoming, min(1024*1024, item.size-total))
                                _require(body, 'artifact_invalid'); target.write(body); digest.update(body); total += len(body)
                        _require(digest.hexdigest() == item.sha256 and _file_stat_signature(os.fstat(incoming)) == item.stat_signature,
                                 'artifact_invalid')
                    finally: os.close(incoming)
            stream.flush(); os.fsync(stream.fileno())
        _require(parse_ios_artifact(artifact._source).app_digest == artifact.app_digest, 'artifact_invalid')
        size = os.fstat(output).st_size; _require(0 < size <= MAX_TRANSFER_BYTES)
        body = bytearray(); offset = 0
        while offset < size:
            block = os.pread(output, min(1024*1024, size-offset), offset); _require(block)
            body.extend(block); offset += len(block)
        return BlobSet((('candidate.ipa', bytes(body)),))
    finally: os.close(output)


def _clean_failure(operations, descriptor, intent, context, stop, deadline, error):
    with operations._mutex: _require(not operations._native_controls, 'ios_signing_quarantined')
    _, state = operations._records(descriptor, context.operation_id)
    operations._cleanup(descriptor, intent, state)
    code = ('cancelled' if stop.is_set() else 'signing_timeout' if time.monotonic() >= deadline
            else 'signature_invalid' if context.signed_artifact_digest is not None else 'signing_failed')
    if getattr(error, 'code', None) == 'artifact_invalid': code = 'artifact_invalid'
    evidence = contracts.digest({'context': context.digest, 'definition': operations.definition_digest, 'failure': code})
    return SigningFailureObservation(context.digest, code, evidence, True, True)


def execute_sign(operations, operation, artifacts, *, material_resolver, provisioning, policy_document,
                 cancellation, deadline_monotonic):
    _require(type(material_resolver) is IOSSigningMaterialResolver)
    context = operation.context
    operations.stage(operation, artifacts)
    with _execution(operations, operation, context, provisioning, policy_document, cancellation, deadline_monotonic, 'sign') as (fd, intent, session, stop):
        try:
            _, state = operations._records(fd, operation.operation_id)
            _clear_transfer(fd, intent)
            artifact = _inject_profiles(operations, fd, intent, state)
            upper = _check_native_capacity(operations, artifact, state['inputBytes'])
            profile_evidence = _verify_profiles(operations, session, provisioning, context, stop, deadline_monotonic, artifact.app_digest)
            _active(stop, deadline_monotonic)
            _require(parse_ios_artifact(session.root/'App.app').app_digest == artifact.app_digest, 'artifact_invalid')
            with material_resolver.open(operations.definition.identity) as material:
                result = session.sign_process(material, cancellation=stop, deadline_monotonic=deadline_monotonic,
                    app_digest=artifact.app_digest)
            _require(result.terminated, 'ios_signing_quarantined')
            _require(result.returncode == 0 and result.bounded and not result.interrupted)
            native = json.loads(result.stdout)
            _require(native['signedCodeObjects'] == len(operations.definition.code_objects))
            signed_app = parse_ios_artifact(session.root/'App.app')
            _require(signed_app.container_bytes <= upper, 'artifact_invalid')
            signed = _export_app(operations, fd, intent, signed_app)
            _active(stop, deadline_monotonic)
            _, state = operations._records(fd, operation.operation_id)
            state['execution'].update(signedDigest=hashlib.sha256(signed.entries[0][1]).hexdigest(),
                signedBytes=len(signed.entries[0][1]), complete=True)
            evidence = contracts.digest({'context': context.digest, 'profileEvidence': profile_evidence,
                'history': state['execution']['historyDigest'], 'signedArtifact': state['execution']['signedDigest']})
            _replace_at(fd, 'state.json', state)
            operations._cleanup(fd, intent, state)
            return SigningObservation(context.digest, signed, evidence, True, True)
        except Exception as error:
            return _clean_failure(operations, fd, intent, context, stop, deadline_monotonic, error)


def execute_inspection(operations, operation, context, artifacts, *, provisioning, policy_document,
                       cancellation, deadline_monotonic):
    _require(context.signed_artifact_digest is not None)
    with _execution(operations, operation, context, provisioning, policy_document, cancellation, deadline_monotonic, 'inspect') as (fd, intent, session, stop):
        try:
            artifact = operations._stage(operation, artifacts, _expected_digest=context.signed_artifact_digest, _already_locked=True)
            _clear_transfer(fd, intent)
            profile_evidence = _verify_profiles(operations, session, provisioning, context, stop, deadline_monotonic, artifact.app_digest)
            verifier_tools = IOSCodeSignatureTools(Path('/usr/bin/codesign'),
                hashlib.sha256(Path('/usr/bin/codesign').read_bytes()).hexdigest(), operations.tools.sandbox_sha256,
                operations.tools.verifier, operations.tools.verifier_sha256)
            inspector = IOSCodeSignatureInspector(verifier_tools, session.root/'checks'/'signature', signature_kind='identity',
                bundle_policies=operations.definition.bundle_policies,
                expected_certificate_sha256=operations.definition.identity.certificate_sha256,
                expected_team_id=operations.definition.identity.team_id, _process_owner=session.facade())
            try:
                proof = inspector.inspect(artifact, context_digest=context.digest, cancellation=stop, deadline_monotonic=deadline_monotonic)
                signature_evidence = contracts.digest(inspector.require_verified(proof, artifact, context_digest=context.digest))
            finally: _require(inspector.close(deadline_monotonic=time.monotonic()+5), 'ios_signing_quarantined')
            _active(stop, deadline_monotonic)
            _, state = operations._records(fd, operation.operation_id)
            state['execution']['complete'] = True
            evidence = contracts.digest({'context': context.digest, 'signature': signature_evidence,
                'profiles': profile_evidence, 'history': state['execution']['historyDigest']})
            _replace_at(fd, 'state.json', state); operations._cleanup(fd, intent, state)
            return SignatureObservation(context.digest, True, evidence, True, True)
        except Exception as error:
            return _clean_failure(operations, fd, intent, context, stop, deadline_monotonic, error)
