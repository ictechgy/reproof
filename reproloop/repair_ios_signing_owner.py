"""Concrete iOS callbacks backed by native process and recovery ownership."""
from contextlib import contextmanager
import hashlib
import json
import threading
import time

from . import contracts
from .execution.artifacts import BlobSet
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_signing_inputs import IOSSigningMaterialResolver, IOSSigningProvisioning
from .ios_signing_operation import IOSSigningOperation, IOSSigningOperationStore
from .repair_execution import _require
from .repair_signing import SigningContext
from .repair_signing_recovery import _context_common


class _IOSSigningCallback:
    def __init__(self, operations, provisioning, policy_document):
        _require(type(operations) is IOSSigningOperationStore and not operations.recovery_only and operations.tools.guardian is not None
            and type(provisioning) is IOSSigningProvisioning, 'signing_unqualified')
        policy = operations.definition.validate_policy(policy_document)
        self.operations, self.provisioning = operations, provisioning
        self.policy_digest = contracts.digest(policy)
        self._policy = json.dumps(policy, sort_keys=True, separators=(',', ':'))
        self._closed = False
        self._callbacks = 0
        self._changed = threading.Condition(threading.RLock())

    @property
    def active_processes(self):
        with self._changed:
            return self._callbacks + len(self.operations._native_controls)

    def ready(self):
        _require(not self._closed and not self.operations._closed, 'signing_unavailable')
        self.operations.tools.verify(); self.provisioning.tools.verify()

    @contextmanager
    def _callback(self, context, artifacts, signed):
        with self._changed:
            _require(not self._closed, 'signing_unavailable'); self._callbacks += 1
        try:
            _require(type(context) is SigningContext and type(context._operation_binding) is IOSSigningOperation
                and context.signing_policy_digest == self.policy_digest, 'signing_unqualified')
            operation = context._operation_binding
            self.operations._require_operation(operation)
            _require(_context_common(context) == _context_common(operation.context)
                and (context.signed_artifact_digest is not None) is signed
                and self.accepts(artifacts), 'signature_invalid')
            measured = hashlib.sha256(artifacts.entries[0][1]).hexdigest()
            _require(measured == (context.signed_artifact_digest if signed else context.unsigned_artifact_digest),
                     'signature_invalid')
            yield operation
        finally:
            with self._changed: self._callbacks -= 1; self._changed.notify_all()

    @staticmethod
    def accepts(artifacts):
        # Inert transfer admission only. The journal performs bounded extraction,
        # provisioning and independent native inspection before any signed proof.
        return (type(artifacts) is BlobSet and len(artifacts.entries) == 1
            and artifacts.entries[0][0] == 'candidate.ipa'
            and 22 <= len(artifacts.entries[0][1]) <= MAX_TRANSFER_BYTES
            and artifacts.entries[0][1].startswith(b'PK\x03\x04'))

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+5 if deadline_monotonic is None else deadline_monotonic
        self._closed = True
        stopped = self.operations.close(deadline_monotonic=deadline)
        with self._changed:
            while self._callbacks and time.monotonic() < deadline:
                self._changed.wait(max(0, deadline-time.monotonic()))
            return stopped and self._callbacks == 0


class IOSSigningOwnerSigner(_IOSSigningCallback):
    def __init__(self, operations, resolver, provisioning, policy_document):
        _require(type(resolver) is IOSSigningMaterialResolver, 'signing_unqualified')
        super().__init__(operations, provisioning, policy_document)
        self.resolver = resolver

    def __call__(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context, artifacts, False) as operation:
            return self.operations.sign(operation, artifacts, material_resolver=self.resolver,
                provisioning=self.provisioning, policy_document=json.loads(self._policy),
                cancellation=cancellation, deadline_monotonic=deadline_monotonic)


class IOSSigningOwnerInspector(_IOSSigningCallback):
    def __call__(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context, artifacts, True) as operation:
            return self.operations.inspect(operation, context, artifacts, provisioning=self.provisioning,
                policy_document=json.loads(self._policy), cancellation=cancellation,
                deadline_monotonic=deadline_monotonic)


__all__ = ['IOSSigningOwnerSigner', 'IOSSigningOwnerInspector']
