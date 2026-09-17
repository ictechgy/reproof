"""Fixed signing callbacks backed by the durable, lock-holding JVM owner."""
from contextlib import contextmanager
import threading
import time

from .execution.wire import MAX_TRANSFER_BYTES
from .repair_android_signing import (
    AndroidSigningMaterialResolver, _artifact_body, _structural_apk,
    _validated_policy, _validate_context,
)
from .repair_execution import _require
from .repair_signing import SigningContext
from .repair_signing_recovery import SigningOperation, SigningOperationStore


class _SigningOwnerCallback:
    def __init__(self, operations, policy_document, max_apk_bytes):
        _require(type(operations) is SigningOperationStore
                 and type(max_apk_bytes) is int and 0 < max_apk_bytes <= MAX_TRANSFER_BYTES,
                 'signing_unqualified')
        self.operations = operations
        self.tools = operations.tools
        self.identity = operations.identity
        _, self.policy_digest = _validated_policy(self.identity, policy_document)
        self.max_apk_bytes = max_apk_bytes
        self._changed = threading.Condition(threading.RLock())
        self._closed = False
        self._callbacks = 0

    @property
    def active_processes(self):
        with self._changed:
            return self._callbacks + self.operations.active_processes

    def ready(self):
        with self._changed:
            _require(not self._closed and not self.operations._closed, 'signing_unavailable')
        self.tools.verify()

    @contextmanager
    def _callback(self, context, artifacts, mode):
        with self._changed:
            _require(not self._closed, 'signing_unavailable')
            self._callbacks += 1
        try:
            _require(type(context) is SigningContext
                     and type(context._operation_binding) is SigningOperation
                     and context._operation_binding.store is self.operations,
                     'signing_unqualified')
            body = _artifact_body(artifacts, self.max_apk_bytes)
            _validate_context(context, self.identity, self.policy_digest, body,
                              signed_required=mode == 'inspect')
            operation = context._operation_binding
            # Check the live operation before a keystore descriptor is opened;
            # execute() repeats this check under its producer lock.
            self.operations._require_operation(operation, context, mode, body)
            yield operation
        finally:
            with self._changed:
                self._callbacks -= 1
                self._changed.notify_all()

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic() + 5 if deadline_monotonic is None else deadline_monotonic
        self._closed = True
        stopped = self.operations.close(deadline_monotonic=deadline)
        with self._changed:
            while self._callbacks and time.monotonic() < deadline:
                self._changed.wait(max(0, deadline - time.monotonic()))
            return stopped is True and self._callbacks == 0


class AndroidSigningOwnerSigner(_SigningOwnerCallback):
    def __init__(self, operations, resolver, policy_document, *, max_apk_bytes=MAX_TRANSFER_BYTES):
        _require(type(resolver) is AndroidSigningMaterialResolver, 'signing_unqualified')
        super().__init__(operations, policy_document, max_apk_bytes)
        self.resolver = resolver

    def __call__(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context, artifacts, 'sign') as operation:
            material = self.resolver.open(self.identity)
            try:
                return self.operations.execute(operation, context, artifacts, mode='sign',
                    material=material, cancellation=cancellation, deadline=deadline_monotonic)
            finally:
                material.close()


class AndroidSigningOwnerInspector(_SigningOwnerCallback):
    def __init__(self, operations, policy_document, *, max_apk_bytes=MAX_TRANSFER_BYTES):
        super().__init__(operations, policy_document, max_apk_bytes)

    def __call__(self, context, artifacts, *, cancellation, deadline_monotonic):
        with self._callback(context, artifacts, 'inspect') as operation:
            return self.operations.execute(operation, context, artifacts, mode='inspect',
                cancellation=cancellation, deadline=deadline_monotonic)

    def accepts(self, artifacts):
        try:
            return _structural_apk(_artifact_body(artifacts, self.max_apk_bytes), self.max_apk_bytes)
        except (RuntimeError, ValueError, OSError):
            return False


__all__ = ['AndroidSigningOwnerSigner', 'AndroidSigningOwnerInspector']
