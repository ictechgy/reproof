"""Platform recovery finalization bound to one canonical execution journal.

These functions consume one-shot cleanup capabilities issued by the native
operation stores (Android, iOS, signing). They live outside journal.py so the
generic admission journal carries no platform-specific recovery code or
imports; RunStore keeps thin delegating methods for a stable call surface.
"""
from __future__ import annotations

import os

from reproloop.core import ContractError
from reproloop.contracts.versions import require, validate_digest, validate_id
from .artifacts import ArtifactError, open_directory
from .journal import OwnedRun, RunDenied
from .wire import ProtocolError, decode_json


def finish_mobile_recovery(store, capability, *, authority):
    """Consume a live cleanup capability after exact Android staging disposal."""
    from ..android_recovery_finalization import AndroidCleanupCapability
    from ..repair_android_operation import AndroidOperationError, AndroidOperationStore
    if (type(authority) is not AndroidOperationStore or type(capability) is not AndroidCleanupCapability
            or authority.run_store is not store):
        raise RunDenied("Mobile cleanup authority is unavailable")
    parent = directory = None
    try:
        with store._control():
            value = store._load()
            record = value["runs"].get(capability.operation_id)
            if (value.get("scope") != {"kind": "mobile-device", "scopeDigest": authority.config.scope_digest}
                    or record is None or record["requestDigest"] != capability.request_digest
                    or record["state"] not in {"admitted", "quarantined"}):
                raise RunDenied("Mobile recovery binding changed")
            if authority.require_cleanup(capability, store) is not capability:
                raise RunDenied("Mobile cleanup authority is unavailable")
            parent = open_directory(store.root / "runs")
            try:
                directory = os.open(capability.operation_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o077 or os.listdir(directory):
                    raise RunDenied("Mobile run directory cleanup is unknown")
                os.rmdir(capability.operation_id, dir_fd=parent)
                os.fsync(parent)
            run = OwnedRun(store, capability.operation_id, capability.request_digest)
            record["state"] = "cancelled" if run.cancelled() else "failed"
            record["reservedBytes"] = 0
            store._write(value)
            return dict(record)
    except (AndroidOperationError, OSError, ArtifactError, ContractError, ProtocolError):
        raise RunDenied("Mobile cleanup recovery is unavailable") from None
    finally:
        if directory is not None:
            os.close(directory)
        if parent is not None:
            os.close(parent)


def finish_ios_native_recovery(store, capability, *, authority):
    """Consume measured native recovery and exact staged-file disposal."""
    from ..ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
    from ..ios_recovery_finalization import (IOSRecoveryCleanupCapability,
                                              require_ios_recovery_cleanup)
    if (type(authority) is not IOSMobileOperationStore or authority.run_store is not store
            or type(capability) is not IOSRecoveryCleanupCapability):
        raise RunDenied('iOS native cleanup authority is unavailable')
    try:
        validate_id(capability.operation_id)
        validate_digest(capability.request_digest)
        validate_digest(capability.scope_digest)
        validate_digest(capability.context_digest)
        require(type(capability.expected_reserved_bytes) is int
                and capability.expected_reserved_bytes > 0, 'Invalid iOS native reservation')
        with store._control():
            value = store._load()
            record = value['runs'].get(capability.operation_id)
            if (value.get('scope') != {'kind': 'mobile-device',
                                      'scopeDigest': authority.definition.scope_digest}
                    or record is None or record['requestDigest'] != capability.request_digest
                    or record['state'] not in {'admitted', 'quarantined'}
                    or record['reservedBytes'] != capability.expected_reserved_bytes):
                raise RunDenied('iOS native recovery binding changed')
            session = require_ios_recovery_cleanup(authority, capability, store)
            session.remove_run_hold()
            run = OwnedRun(store, capability.operation_id, capability.request_digest)
            record['state'] = 'cancelled' if run.cancelled() else 'failed'
            record['reservedBytes'] = 0
            store._write(value)
            capability._consumed = True
            return dict(record)
    except (IOSMobileOperationError, OSError, ArtifactError, ContractError,
            ProtocolError, TypeError, ValueError, AttributeError):
        raise RunDenied('iOS native cleanup is unavailable') from None


def finish_ios_preparation_recovery(store, capability, *, authority):
    """Release only freshly disposed preparation files, never device ownership."""
    from ..ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore
    from ..ios_mobile_recovery import IOSPreparationCleanupCapability
    if (type(authority) is not IOSMobileOperationStore or authority.run_store is not store
            or type(capability) is not IOSPreparationCleanupCapability):
        raise RunDenied('iOS preparation cleanup authority is unavailable')
    try:
        validate_id(capability.operation_id);validate_digest(capability.request_digest)
        validate_digest(capability.scope_digest);validate_digest(capability.context_digest)
        require(type(capability.expected_reserved_bytes) is int and capability.expected_reserved_bytes > 0,
                'Invalid iOS preparation reservation')
        with store._control():
            value = store._load()
            record = value['runs'].get(capability.operation_id)
            if (value.get('scope') != {'kind':'mobile-device','scopeDigest':authority.definition.scope_digest}
                    or record is None or record['requestDigest'] != capability.request_digest
                    or record['state'] not in {'admitted','quarantined','failed','cancelled'}
                    or (record['reservedBytes'] != capability.expected_reserved_bytes
                        if record['state'] in {'admitted','quarantined'} else record['reservedBytes'] != 0)):
                raise RunDenied('iOS preparation recovery binding changed')
            session = authority.require_preparation_cleanup(capability,store)
            if record['state'] in {'failed','cancelled'}:
                return dict(record)
            session.remove_run_hold()
            run = OwnedRun(store,capability.operation_id,capability.request_digest)
            record['state'] = 'cancelled' if run.cancelled() else 'failed'
            record['reservedBytes'] = 0
            store._write(value)
            return dict(record)
    except (IOSMobileOperationError,OSError,ArtifactError,ContractError,ProtocolError,TypeError,ValueError,AttributeError):
        raise RunDenied('iOS preparation cleanup is unavailable') from None


def consume_ios_native_disposal(store, native_owner, cleanup_token, sanitation, evidence_digest,
                                *, cancellation, deadline_monotonic):
    """Remove the iOS file hold; run accounting and device release remain separate."""
    from ..ios_mobile_native import IOSMobileNativeOwner
    from ..ios_mobile_callbacks import require_cleanup_callback
    from ..ios_mobile_runtime_identity import IOSRuntimeIdentityReadObservation
    from ..ios_mobile_operation import IOSMobileOperationError
    from ..ios_mobile_finalization import discard_native_staged
    from ..repair_android_operation import (_open_child_directory, _open_regular_at, _read_fd,
        _identity_info, _same_identity)
    parent=directory=descriptor=None
    try:
        require(type(native_owner) is IOSMobileNativeOwner and native_owner.operations.run_store is store,
                'Original iOS native owner required')
        native_owner._check()
        require(require_cleanup_callback(native_owner) is cleanup_token,'Original cleanup callback required')
        context=native_owner.operation.context
        require(type(sanitation) is IOSRuntimeIdentityReadObservation and sanitation.sanitation is not None
            and sanitation.source_role=='original' and sanitation.context_digest==context.digest
            and sanitation.native_binding_digest==native_owner.binding_digest
            and sanitation.sanitation.stage=='cleanup'
            and sanitation.sanitation.policy_digest==native_owner.operations.definition.sanitation_policy_digest
            and native_owner._sanitation_results.get((sanitation.launch_payload_digest,'cleanup')) is sanitation,
            'Fresh original app sanitation required')
        validate_digest(evidence_digest)
        require(discard_native_staged(native_owner,cleanup_token,cancellation=cancellation,
            deadline_monotonic=deadline_monotonic)==evidence_digest,'Native disposal evidence changed')
        with store._control():
            value=store._load();record=value['runs'].get(context.operation_id)
            require(value.get('scope')=={'kind':'mobile-device','scopeDigest':context.scope_digest}
                and record is not None and record['requestDigest']==context.request_digest
                and record['state']=='admitted' and record['reservedBytes']>0,
                'iOS run ownership changed')
            parent=open_directory(store.root/'runs')
            directory=_open_child_directory(parent,context.operation_id)
            names=set(os.listdir(directory))
            if names:
                require(names=={'intent.json'},'iOS run hold has unknown contents')
                descriptor=_open_regular_at(directory,'intent.json')
                identity=_identity_info(os.fstat(descriptor))
                require(decode_json(_read_fd(descriptor,4096))=={
                    'kind':'ios-mobile-preparation-hold','contextDigest':context.digest},'iOS hold changed')
                require(_same_identity(os.stat('intent.json',dir_fd=directory,follow_symlinks=False),identity),
                    'iOS hold identity changed')
                os.unlink('intent.json',dir_fd=directory);os.fsync(directory)
                native_owner._disposal_digest=evidence_digest
            else:
                require(native_owner._disposal_digest==evidence_digest,'Unissued iOS hold consumption')
        return evidence_digest
    except (IOSMobileOperationError,OSError,ArtifactError,ContractError,ProtocolError,TypeError,ValueError,AttributeError):
        raise RunDenied('iOS native disposal is unconfirmed') from None
    finally:
        for descriptor in (descriptor,directory,parent):
            if descriptor is not None:os.close(descriptor)


def finish_signing_recovery(store, capability, *, authority):
    """Consume a live native signing cleanup capability under its locks.

    Recovery can only fail or cancel an interrupted operation. The signing
    owner has already proved all native phases stopped and removed their
    exact private files; no VM termination record is accepted here.
    """
    from ..repair_signing_recovery import (
        SigningCleanupCapability, SigningOperationStore, SigningRecoveryError,
    )
    from ..ios_signing_operation import IOSSigningOperationStore
    if (type(authority) not in (SigningOperationStore, IOSSigningOperationStore)
            or type(capability) is not SigningCleanupCapability
            or authority.run_store is not store):
        raise RunDenied('Signing cleanup authority is unavailable')
    parent = directory = None
    try:
        validate_id(capability.operation_id)
        validate_digest(capability.request_digest)
        with store._control():
            value = store._load()
            record = value['runs'].get(capability.operation_id)
            if (value.get('scope') != {'kind': 'signing', 'scopeDigest': authority.scope_digest}
                    or capability.scope_digest != authority.scope_digest
                    or record is None or record['requestDigest'] != capability.request_digest
                    or record['state'] not in {'admitted', 'quarantined'}):
                raise RunDenied('Signing recovery binding changed')
            accepted = authority.require_cleanup(capability, store)
            if accepted is not capability:
                raise RunDenied('Signing cleanup authority is unavailable')
            # Signing keeps its resources in the owner's private journal.
            # Its RunStore directory must be empty, including after a
            # crash between this rmdir and the terminal state write.
            parent = open_directory(store.root / 'runs')
            try:
                directory = os.open(capability.operation_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                info = os.fstat(directory)
                if (info.st_uid != os.getuid() or info.st_mode & 0o077
                        or os.listdir(directory)):
                    raise RunDenied('Signing run directory cleanup is unknown')
                os.rmdir(capability.operation_id, dir_fd=parent)
                os.fsync(parent)
            run = OwnedRun(store, capability.operation_id, capability.request_digest)
            record['state'] = 'cancelled' if run.cancelled() else 'failed'
            record['reservedBytes'] = 0
            store._write(value)
            return dict(record)
    except (SigningRecoveryError, OSError, ArtifactError, ContractError, ProtocolError):
        raise RunDenied('Signing cleanup recovery is unavailable') from None
    finally:
        if directory is not None:
            os.close(directory)
        if parent is not None:
            os.close(parent)
