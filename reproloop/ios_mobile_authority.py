"""Operator rotation of a stale mobile scope authority marker.

The repair scope lease binds the physical scope to the journal's
(stateRoot, environmentDigest) pair through a durable cutover marker. A
legitimate environment rotation — regenerating the protected
configuration — leaves the marker pointing at the retired root, and every
later lease entry fails closed with a canonical-root mismatch. That gate
is correct: it stops a different environment's journal from silently
reinterpreting the same physical scope. But an acknowledged rotation had
no sanctioned path; the only option was deleting tmpdir files by hand.

This module provides that path. It never edits journal state: it verifies
the journal shows no in-flight work, requires the operator to supply the
marker value being retired (the same "original request" style as
close-run), and then removes the stale marker so the next lease entry
rebinds to the current environment. A marker in ``rollback-blocked``
state is refused — that state belongs to the device-authority migration
gate and is not a scope rotation artifact.
"""
import math
import os
import time

from . import contracts
from .execution.journal import TERMINAL, RunDenied
from .ios_mobile_operation import IOSMobileOperationError, IOSMobileOperationStore, _require
from .storage import Lease


def _authority_root(owner):
    run_store = owner.run_store
    return contracts.digest({'stateRoot': str(run_store.root.resolve()),
                             'environmentDigest': run_store.environment_digest})


def _scope_lease(owner):
    return Lease('protected-repair-mobile-device-' + owner.definition.scope_digest,
                 authority_root=_authority_root(owner))


def inspect_scope_authority(owner, *, cancellation, deadline_monotonic):
    """Report the scope marker binding without mutating anything."""
    try:
        _require(type(owner) is IOSMobileOperationStore
            and callable(getattr(cancellation, 'is_set', None))
            and type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic)
            and not owner._closed and not cancellation.is_set()
            and time.monotonic() < deadline_monotonic)
        lease = _scope_lease(owner)
        marker = lease._read_marker()
        return {'markerState': None if marker is None else marker['state'],
                'markerAuthorityRoot': None if marker is None else marker['authorityRoot'],
                'computedAuthorityRoot': _authority_root(owner)}
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSMobileOperationError() from None


def rotate_scope_authority(owner, *, expected_authority_root,
                           cancellation, deadline_monotonic):
    """Retire the stale marker once the journal proves no in-flight work."""
    try:
        _require(type(owner) is IOSMobileOperationStore
            and callable(getattr(cancellation, 'is_set', None))
            and type(deadline_monotonic) in (int, float) and math.isfinite(deadline_monotonic)
            and not owner._closed and not cancellation.is_set()
            and time.monotonic() < deadline_monotonic)
        contracts.validate_digest(expected_authority_root)
        run_store = owner.run_store
        lease = _scope_lease(owner)
        marker = lease._read_marker()
        _require(marker is not None and marker['state'] == 'shared')
        _require(marker['authorityRoot'] == expected_authority_root
                 and marker['authorityRoot'] != _authority_root(owner))
        # A mismatched marker cannot be held by a live lease — entry fails
        # the cutover check — so unlinking cannot race a holder.
        with run_store._control():
            value = run_store._load()
            if any(row['state'] not in TERMINAL for row in value['runs'].values()):
                raise RunDenied('Execution scope is busy or quarantined')
            scope = value.get('scope')
            _require(scope is None or scope == {
                'kind': 'mobile-device',
                'scopeDigest': owner.definition.scope_digest})
        for name in (lease.marker.name, lease.key + '.lock'):
            path = lease.directory / name
            if path.is_file() and not path.is_symlink() \
                    and path.stat().st_uid == os.getuid():
                path.unlink()
        return {'rotated': True, 'retiredAuthorityRoot': marker['authorityRoot'],
                'computedAuthorityRoot': _authority_root(owner)}
    except RunDenied:
        raise
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError):
        raise IOSMobileOperationError() from None
