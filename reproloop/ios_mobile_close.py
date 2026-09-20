"""Operator close of a non-terminal iOS mobile run after device cleanup.

This is the sanctioned replacement for editing the journal by hand. It keeps
every invariant the measured recovery paths keep: the scope lease and locks
are taken, the run/operation binding is verified against durable records,
only the exact run hold and the bound operation directory are removed, and
the terminal state is written atomically before the reservation is released.

The device side is deliberately not re-measured here: an operation that
reached native dispatch may have left device state, so closing it requires
an explicit operator attestation. Runs that never reached dispatch need no
attestation because the device was never touched.
"""
from contextlib import ExitStack
import fcntl
import math
import os
import stat
import time

from . import contracts
from .execution.journal import OwnedRun
from .ios_code_signature import _remove_owned_contents
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore, _require
from .repair_android_operation import (_identity_info, _open_child_directory, _open_regular_at,
    _read_json_at, _retire_record_temps, _same_identity, _walk_directory)

_ENTRY_LIMIT = 65536


def _member(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def close_run(owner, operation_id, request_digest, *, device_clean_attested=False,
              cancellation, deadline_monotonic):
    """Close one non-terminal mobile run once device state is already resolved."""
    try:
        _require(type(owner) is IOSMobileOperationStore
            and type(device_clean_attested) is bool
            and callable(getattr(cancellation, 'is_set', None))
            and type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic)
            and not owner._closed and not cancellation.is_set()
            and time.monotonic() < deadline_monotonic)
        contracts.validate_id(operation_id)
        contracts.validate_digest(request_digest)
        with ExitStack() as stack:
            stack.enter_context(owner.run_store.repair_scope_lease(
                'mobile-device', owner.definition.scope_digest))
            run_root = _walk_directory(owner.run_store.root)
            stack.callback(os.close, run_root)
            vm = _open_regular_at(run_root, '.vm-lock', writable=True)
            stack.callback(os.close, vm)
            fcntl.flock(vm, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runs = _open_child_directory(run_root, 'runs')
            stack.callback(os.close, runs)
            operations_root = _walk_directory(owner.operations)
            stack.callback(os.close, operations_root)
            _require(not cancellation.is_set() and time.monotonic() < deadline_monotonic)

            # Bind the operation directory before removing anything. A missing
            # directory means an earlier close attempt already removed it; the
            # journal record then remains the only binding left to check.
            info = _member(operations_root, operation_id)
            directory = intent = None
            if info is not None:
                _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid())
                directory = stack.enter_context(owner._directory(operation_id))
                intent, state = owner._records(operation_id, directory)
                _require(intent['requestDigest'] == request_digest)
                producer = _open_regular_at(
                    directory, 'producer.lock', expected=intent['producerIdentity'], writable=True)
                stack.callback(os.close, producer)
                fcntl.flock(producer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                native_bound = ('nativeBindingDigest' in state
                                or 'native.json' in os.listdir(directory))
                _require(not native_bound or device_clean_attested)

            row = owner.run_store.status(operation_id)
            _require(row['requestDigest'] == request_digest
                and row['state'] in {'admitted', 'quarantined'}
                and (intent is None or row['reservedBytes'] == intent['reservedBytes']))
            with owner._changed:
                _require(not owner._closed and not owner._active and not owner._callbacks)

            # The run hold may contain only its one documented marker.
            run_identity = None
            run_info = _member(runs, operation_id)
            if run_info is not None:
                _require(stat.S_ISDIR(run_info.st_mode))
                run_directory = _open_child_directory(runs, operation_id)
                stack.callback(os.close, run_directory)
                run_identity = _identity_info(os.fstat(run_directory))
                _retire_record_temps(run_directory)
                names = set(os.listdir(run_directory))
                _require(names <= {'intent.json'})
                if names:
                    hold = _read_json_at(run_directory, 'intent.json')
                    _require(type(hold) is dict
                        and hold.get('kind') == 'ios-mobile-preparation-hold'
                        and set(hold) == {'kind', 'contextDigest'})
                    if intent is not None:
                        _require(hold['contextDigest'] == intent['contextDigest'])
                    else:
                        contracts.validate_digest(hold['contextDigest'])

            if directory is not None:
                remaining = [_ENTRY_LIMIT]
                _remove_owned_contents(
                    directory, remaining, deadline_monotonic=deadline_monotonic)
                _require(_same_identity(
                    os.stat(operation_id, dir_fd=operations_root, follow_symlinks=False),
                    intent['directoryIdentity'], directory=True))
                os.rmdir(operation_id, dir_fd=operations_root)
                os.fsync(operations_root)
            if run_identity is not None:
                names = set(os.listdir(run_directory))
                _require(names <= {'intent.json'})
                if names:
                    os.unlink('intent.json', dir_fd=run_directory)
                    os.fsync(run_directory)
                _require(_same_identity(
                    os.stat(operation_id, dir_fd=runs, follow_symlinks=False),
                    run_identity, directory=True))
                os.rmdir(operation_id, dir_fd=runs)
                os.fsync(runs)

            with owner.run_store._control():
                value = owner.run_store._load()
                record = value['runs'].get(operation_id)
                _require(value.get('scope') == {
                    'kind': 'mobile-device', 'scopeDigest': owner.definition.scope_digest}
                    and record is not None and record['requestDigest'] == request_digest
                    and record['state'] in {'admitted', 'quarantined'})
                run = OwnedRun(owner.run_store, operation_id, request_digest)
                record['state'] = 'cancelled' if run.cancelled() else 'failed'
                record['reservedBytes'] = 0
                owner.run_store._write(value)
                return dict(record)
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSMobileOperationError() from None
