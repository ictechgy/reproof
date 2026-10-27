"""Process-owned Android app preparation; serialized receipts are never capabilities.

Only the opted-in Debug app can acknowledge its declared fixture. This is not
an assertion about production endpoints, remote data, or defect correctness.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import hashlib
import threading
import time

from .. import contracts
from ..fixtures import (AdapterCapabilities, FixtureCoordinator, FixtureError,
                        FixtureOperationRequest, FixtureOperationResult)


def _require(value, message='Android preparation binding is unavailable'):
    if not value:
        raise FixtureError('android_preparation_invalid', message)


def _operation(*parts):
    return 'operation_' + hashlib.sha256('\0'.join(parts).encode()).hexdigest()[:40]


def android_fixture_endpoint(configuration):
    """Fixed build-independent identity for the declared fixture contract.

    The G0 identifier limit is 64 characters: an alphabetic prefix plus 252
    digest bits. Full fixture/version/policy bindings remain in every challenge.
    """
    from ..android_readiness import ApprovedAndroidFixture, android_fixture_endpoint_for_policy
    _require(type(configuration) is ApprovedAndroidFixture)
    value=configuration.data
    return android_fixture_endpoint_for_policy(fixture_id=value['fixtureId'],
        fixture_version=value['fixtureVersion'], policy_digest=value['policyDigest'])


class AndroidFixtureAdapter:
    """A fixed in-process adapter, bound once to an authenticated native provider.

    The native SDK must retain operation identities for at least 60 seconds.
    Host retention starts at dispatch (not each status response), and therefore
    cannot extend that native promise. No arbitrary adapter/path is loaded.
    """
    capabilities = AdapterCapabilities(True, True, 60_000)

    def __init__(self, configuration):
        from ..android_readiness import ApprovedAndroidFixture
        _require(type(configuration) is ApprovedAndroidFixture)
        self._configuration = configuration
        self._context = None
        self._lock = threading.RLock()
        self._dispatched = {}

    @property
    def configuration(self):
        return self._configuration

    @property
    def endpoint_id(self):
        return android_fixture_endpoint(self._configuration)

    def _bind(self, context):
        with self._lock:
            _require(self._context is None or self._context is context
                     or self._context._state == 'cleaned')
            if self._context is not context:
                self._dispatched.clear()
            self._context = context

    def _call(self, request, timeout_seconds, *, status_only):
        _require(type(request) is FixtureOperationRequest and request.payload == {}
                 and type(request.payload) is dict)
        _require(type(timeout_seconds) in (int, float) and not isinstance(timeout_seconds, bool)
                 and 0 < timeout_seconds <= 60)
        with self._lock:
            context = self._context
            _require(context is not None and context.provider is not None)
            context.require_dispatch(request, cleanup=request.operation == 'cleanup')
            operation = request.wire(include_payload=False)
            old = self._dispatched.get(request.operation_id)
            if old is not None:
                _require(old[0] == operation)
                # A coordinator replay may inspect, but never re-execute, an
                # already dispatched effect, even after a lost response.
                status_only = True
            else:
                _require(not status_only)
                old = (copy.deepcopy(operation), int(time.time() * 1000))
                self._dispatched[request.operation_id] = old
            expires = old[1] + self.capabilities.idempotency_retention_ms
            _require(int(time.time() * 1000) < expires)
            challenge = context.challenge
        result = context.provider.fixture_operation(
            operation, challenge, timeout_seconds, status_only=status_only)
        _require(type(result) is dict and set(result) == {
            'challenge', 'operation', 'phase', 'status', 'quiescent'})
        _require(result['challenge'] == challenge and result['operation'] == operation
                 and result['phase'] == request.operation
                 and result['status'] in {'complete', 'failed', 'unknown'}
                 and type(result['quiescent']) is bool)
        terminal = result['status'] in {'complete', 'failed'}
        _require(not terminal or result['quiescent'] is True)
        now = int(time.time() * 1000)
        status = result['status'] if now < expires else 'expired'
        return FixtureOperationResult(request.operation_id, request.generation,
            status, now if terminal else None, expires if terminal else None,
            request.generation)

    def execute(self, request, *, timeout_seconds):
        return self._call(request, timeout_seconds, status_only=False)

    def status(self, request, *, timeout_seconds):
        return self._call(request, timeout_seconds, status_only=True)


@dataclass(frozen=True, slots=True, init=False)
class AndroidStartupHandoff:
    """One-use, process-owned capability. A dict/receipt cannot substitute it."""
    _issuer: object
    _context: object
    provider: object
    session_id: str
    configuration_digest: str
    receipt_digest: str
    _challenge_bytes: bytes

    def __init__(self, issuer, context, provider, session_id, receipts, challenge):
        values={'_issuer':issuer,'_context':context,'provider':provider,
                'session_id':session_id,'configuration_digest':context.configuration.digest,
                'receipt_digest':contracts.digest(receipts),
                '_challenge_bytes':json.dumps(challenge,sort_keys=True,separators=(',',':'),
                                             allow_nan=False).encode('utf-8')}
        for key,value in values.items():object.__setattr__(self,key,value)

    @property
    def challenge(self):
        return json.loads(self._challenge_bytes)

    def __reduce__(self):
        raise TypeError('Android startup capabilities cannot be serialized')


class AndroidPreparation:
    """One attempt owned by IssueSessionService and its reserved native session."""
    def __init__(self, fixtures, plan, *, registration, configuration, issue_id, timeout_seconds,
                 cancellation, effect_authorizer=None, on_progress=None):
        from ..android_readiness import ApprovedAndroidFixture
        _require(type(fixtures) is FixtureCoordinator
                 and type(configuration) is ApprovedAndroidFixture
                 and type(plan.adapter) is AndroidFixtureAdapter
                 and plan.adapter.configuration is configuration
                 and len(plan.check_recipe_ids) == 1)
        fixtures.require_plan(plan)
        from .recording_session import TrustedProjectRegistration
        _require(type(registration) is TrustedProjectRegistration
                 and registration.project_digest == plan.project_digest
                 and plan.fixture_id == configuration.data['fixtureId'])
        recipes={item['id']:item for item in registration.project['fixtures']}
        _require(all(recipes[identifier]['endpointId'] == android_fixture_endpoint(configuration)
                     for identifier in (plan.fixture_id,*plan.check_recipe_ids,plan.cleanup_recipe_id)))
        contracts.validate_id(issue_id)
        _require(type(timeout_seconds) in (int, float) and not isinstance(timeout_seconds, bool)
                 and 0 < timeout_seconds <= 60)
        _require(callable(getattr(cancellation, 'is_set', None)))
        self.fixtures, self.plan, self.configuration = fixtures, plan, configuration
        self.issue_id, self.timeout_seconds = issue_id, timeout_seconds
        self.cancellation, self.effect_authorizer = cancellation, effect_authorizer
        self.on_progress = on_progress
        self.provider = self.session = self.lab = self.allocation = None
        self._challenge = None
        self._receipts = []
        self._issuer = object()
        self._handoff = None
        self._state = 'new'
        self._lock = threading.RLock()
        self.cleanup_result = None
        self._cleanup_started = False

    @property
    def challenge(self):
        return copy.deepcopy(self._challenge)

    @property
    def receipts(self):
        return copy.deepcopy(self._receipts)

    def reserve(self, allocation):
        with self._lock:
            _require(self._state == 'new' and self.allocation is None)
            self.allocation = allocation
            self._state = 'reserved'
            self.plan.adapter._bind(self)

    def _authorize(self, kind, *, cleanup=False):
        if not cleanup:
            _require(not self.cancellation.is_set(), 'Android preparation was cancelled')
        if self.effect_authorizer is not None:
            _require(self.effect_authorizer(kind) is True, 'Android preparation authority was revoked')
        if self.session is not None:
            handle = self.session.get('deviceAuthority')
            _require(handle is not None)
            handle.check_ownership()

    def require_dispatch(self, request, *, cleanup):
        _require(self.allocation is not None and request.allocation_id == self.allocation.allocation_id
                 and request.generation == self.allocation.generation
                 and request.recipe_id == {'prepare': self.plan.fixture_id,
                     'check': self.plan.check_recipe_ids[0],
                     'cleanup': self.plan.cleanup_recipe_id}.get(request.operation))
        self._authorize('fixture_' + request.operation, cleanup=cleanup)
        _require(self._state in ({'cleaning'} if cleanup else {'preparing'}))

    def prepare(self, provider, session, lab):
        with self._lock:
            _require(self._state == 'reserved' and self.provider is None)
            _require(session.get('deviceAuthority') is not None
                     and session.get('_androidPreparation') is self
                     and session.get('state') == 'connecting'
                     and not session.get('releaseRecorder'))
            self.provider, self.session, self.lab = provider, session, lab
            self._state = 'preparing'
            self._authorize('fixture_prepare')
            self._challenge = provider.preparation_challenge(
                session, project_digest=self.plan.project_digest,
                build_id=session['_androidRelease']['buildId'])
            _require(type(self._challenge) is dict)
        outcome = self.fixtures.prepare(self.plan, self.allocation, payload={},
            operation_id=_operation(self.issue_id, self.plan.fixture_id, 'prepare'),
            timeout_seconds=self.timeout_seconds, effect_authorizer=self.effect_authorizer)
        with self._lock:
            self._receipts = copy.deepcopy(list(outcome.receipts))
            if self.on_progress is not None:
                self.on_progress(self.receipts)
            _require(outcome.status == 'complete')
            self._authorize('fixture_ready')
            _require(self._state == 'preparing' and session.get('state') == 'connecting'
                     and not session.get('stopAdmission', False))
            self._state = 'ready'
            self._handoff = AndroidStartupHandoff(self._issuer, self, provider,
                session['id'], self._receipts, self._challenge)
            return self._handoff

    def consume(self, handoff, provider, session):
        with self._lock:
            _require(type(handoff) is AndroidStartupHandoff and handoff is self._handoff
                     and handoff._issuer is self._issuer and handoff._context is self
                     and self._state == 'ready' and provider is self.provider
                     and session is self.session and handoff.provider is provider
                     and handoff.session_id == session['id']
                     and handoff.receipt_digest == contracts.digest(self._receipts)
                     and handoff.configuration_digest == self.configuration.digest
                     and handoff.challenge == self._challenge)
            self._authorize('fixture_handoff')
            _require(session['state'] == 'connecting' and not session.get('stopAdmission', False))
            _require(self.fixtures.status(self.allocation)['state'] == 'ready')
            _require(provider.validate_android_preparation(handoff) is True)
            self._authorize('fixture_handoff')
            _require(self._state == 'ready' and session['state'] == 'connecting'
                     and not session.get('stopAdmission', False)
                     and self.fixtures.status(self.allocation)['state'] == 'ready')
            self._handoff = None  # Consume before recorder/activation; failures cannot retry.
            self._state = 'consumed'
            return self.receipts

    def admitted(self):
        with self._lock:
            self._authorize('fixture_activate')
            _require(self._state == 'consumed')
            self._state = 'active'

    def abandon_cleanup(self):
        with self._lock:
            self._cleanup_started=True
            self._handoff=None
            self._state='unknown'
        if self.allocation is not None:
            self.fixtures.retain_for_cleanup(self.allocation)

    def quiesce_and_cleanup(self, provider):
        with self._lock:
            if self._state == 'cleaned':
                return True
            if self._cleanup_started:
                return False
            self._cleanup_started = True
            self._handoff = None
            self._state = 'cleaning'
        try:
            # A timeout/unbind/closed transport is never producer quiescence.
            _require(provider is self.provider and
                     provider.quiesce_android_preparation(self.timeout_seconds) is True)
            self._authorize('fixture_cleanup', cleanup=True)
            operation_id = _operation(self.issue_id, self.plan.fixture_id, 'cleanup',
                                      str(self.allocation.generation))
            result = self.fixtures.cleanup(self.plan, self.allocation,
                operation_id=operation_id, timeout_seconds=self.timeout_seconds)
            complete = (result['status'] == 'complete' and
                        self.fixtures.status(self.allocation)['state'] == 'available')
            self.cleanup_result = result
            with self._lock:
                complete=complete and self._state == 'cleaning'
                self._state = 'cleaned' if complete else 'unknown'
            return complete
        except Exception:
            with self._lock:
                self._state = 'unknown'
            if self.allocation is not None:
                try:self.fixtures.retain_for_cleanup(self.allocation)
                except Exception:pass
            return False
