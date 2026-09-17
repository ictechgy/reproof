"""Independent trusted checks. Candidate reports are never validation authority."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import secrets
import threading
import time

from . import contracts
from .execution.protocol import validate_external_validation_plan
from .execution.wire import canonical


class ValidationError(RuntimeError):
    def __init__(self, code='validation_invalid'):
        super().__init__('Independent validation was rejected')
        self.code = code


def _require(condition, code='validation_invalid'):
    if not condition:
        raise ValidationError(code)


@dataclass(frozen=True, slots=True)
class ValidationBinding:
    operation_id: str
    repair_plan_digest: str
    project_digest: str
    source_digest: str
    artifact_digest: str

    def __post_init__(self):
        try:
            contracts.validate_id(self.operation_id)
            for value in (self.repair_plan_digest, self.project_digest,
                          self.source_digest, self.artifact_digest):
                contracts.validate_digest(value)
        except contracts.ContractError:
            raise ValidationError() from None

    def public(self):
        return {'operationId': self.operation_id, 'repairPlanDigest': self.repair_plan_digest,
                'projectDigest': self.project_digest, 'sourceDigest': self.source_digest,
                'artifactDigest': self.artifact_digest}


@dataclass(frozen=True, slots=True)
class ValidationContext:
    binding: ValidationBinding
    validation_plan_digest: str
    check_id: str
    recipe_id: str
    evidence_source_id: str
    nonce: str = field(repr=False)

    def public(self):
        return {**self.binding.public(), 'validationPlanDigest': self.validation_plan_digest,
                'checkId': self.check_id, 'recipeId': self.recipe_id,
                'evidenceSourceId': self.evidence_source_id, 'nonce': self.nonce}

    @property
    def digest(self):
        return contracts.digest(self.public())


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    context_digest: str
    outcome: str
    evidence_digest: str
    termination_confirmed: bool
    cleanup_confirmed: bool


@dataclass(frozen=True, slots=True)
class TrustedValidationReceipt:
    receipt_id: str
    _document: str = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def public(self):
        return json.loads(self._document)


class _Cancellation:
    def __init__(self, parent):
        self.parent = parent
        self.stopped = threading.Event()

    def is_set(self):
        return self.parent.is_set() or self.stopped.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            self.stopped.wait(.01 if deadline is None else max(0, min(.01, deadline - time.monotonic())))
        return self.is_set()


class TrustedValidationAuthority:
    """Register fixed local observers/runners; never accept callbacks over RPC.

    Each adapter must independently observe the bound candidate or control its
    own protected harness, and confirm its termination and cleanup. Parsing a
    candidate's XML/JSON into ValidationObservation violates this contract.
    An unresponsive adapter quarantines this authority; late replies cannot
    publish a receipt. Host adapters never execute candidate source or binaries.
    """
    def __init__(self, plan):
        try:
            plan = validate_external_validation_plan(plan)
        except (contracts.ContractError, TypeError, ValueError):
            raise ValidationError() from None
        self._plan_json = canonical(plan).decode('utf-8')
        self.definition_digest = contracts.digest(plan)
        self.project_digest = plan['projectDigest']
        self._issuer = object()
        self._lock = threading.RLock()
        self._sources = {}
        self._operations = set()
        self._receipts = {}
        self._sealed = False
        self._quarantined = False

    @property
    def plan(self):
        return json.loads(self._plan_json)

    def register(self, source_id, observer, *, kind):
        try:
            contracts.validate_id(source_id)
        except contracts.ContractError:
            raise ValidationError() from None
        _require(kind in {'trusted-runner', 'external-observation'} and callable(observer))
        _require(source_id in {check['evidenceSourceId'] for check in self.plan['checks']})
        with self._lock:
            _require(not self._sealed and source_id not in self._sources, 'validation_source_conflict')
            self._sources[source_id] = (kind, observer)

    def ready(self, *, project_digest=None, recipe_ids=None):
        plan = self.plan
        with self._lock:
            _require(not self._quarantined, 'validation_quarantined')
            _require(project_digest is None or project_digest == self.project_digest, 'validation_binding')
            _require(recipe_ids is None or set(recipe_ids) == {item['recipeId'] for item in plan['checks']},
                     'validation_recipe_mismatch')
            for check in plan['checks']:
                source = self._sources.get(check['evidenceSourceId'])
                _require(source is not None and source[0] == check['kind'], 'validation_source_unavailable')
            return self.definition_digest

    def run(self, binding, *, cancellation, timeout_seconds=120):
        _require(type(binding) is ValidationBinding and callable(getattr(cancellation, 'is_set', None)))
        _require(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 900)
        self.ready(project_digest=binding.project_digest)
        with self._lock:
            _require(binding.operation_id not in self._operations, 'validation_reused')
            _require(len(self._operations) < 1024, 'validation_limit')
            self._operations.add(binding.operation_id)
            self._sealed = True
        stop = _Cancellation(cancellation)
        deadline = time.monotonic() + timeout_seconds
        rows = []
        status = 'pass'
        for check in self.plan['checks']:
            if stop.is_set():
                status = 'cancelled'; break
            if time.monotonic() >= deadline:
                status = 'failed'; break
            context = ValidationContext(binding, self.definition_digest, check['id'],
                check['recipeId'], check['evidenceSourceId'], secrets.token_hex(24))
            observer = self._sources[check['evidenceSourceId']][1]
            ready = threading.Event(); result = []
            def observe(context=context, observer=observer, result=result, ready=ready):
                try:
                    result.append(observer(context, cancellation=stop, deadline_monotonic=deadline))
                except Exception:
                    pass
                finally:
                    ready.set()
            worker = threading.Thread(target=observe, name='repro-independent-validation', daemon=True)
            worker.start()
            while not ready.is_set() and not stop.is_set() and time.monotonic() < deadline:
                ready.wait(min(.01, max(0, deadline - time.monotonic())))
            if not ready.is_set():
                stop.stopped.set()
                status = 'quarantined'
                with self._lock:
                    self._quarantined = True
                rows.append({'checkId': check['id'], 'recipeId': check['recipeId'],
                    'status': 'unknown', 'reason': 'validator_termination_unknown'})
                break
            observed = result[0] if len(result) == 1 else None
            trusted = type(observed) is ValidationObservation
            if trusted:
                try:
                    contracts.validate_digest(observed.evidence_digest)
                    _require(observed.context_digest == context.digest and
                        type(observed.outcome) is str and observed.outcome in {'pass', 'fail', 'unknown'} and
                        type(observed.termination_confirmed) is bool and
                        type(observed.cleanup_confirmed) is bool)
                except (ValidationError, contracts.ContractError):
                    trusted = False
            # Cancellation/deadline forbid a pass. A returning callback still
            # has to prove cleanup before this authority can be reused.
            interrupted = stop.is_set() or time.monotonic() >= deadline
            lifecycle_known = trusted and observed.termination_confirmed and observed.cleanup_confirmed
            passed = lifecycle_known and observed.outcome == 'pass' and not interrupted
            row = {'checkId': check['id'], 'recipeId': check['recipeId'],
                   'contextDigest': context.digest, 'status': 'pass' if passed else 'failed'}
            if trusted:
                row.update(evidenceDigest=observed.evidence_digest, terminationConfirmed=observed.termination_confirmed,
                           cleanupConfirmed=observed.cleanup_confirmed)
            else:
                row['reason'] = 'untrusted_validation_result'
            rows.append(row)
            if not passed:
                status = ('quarantined' if not lifecycle_known else 'cancelled'
                          if cancellation.is_set() else 'failed')
                if status == 'quarantined':
                    with self._lock:
                        self._quarantined = True
                break
        with self._lock:
            if cancellation.is_set() and status != 'quarantined':
                status = 'cancelled'
            elif time.monotonic() >= deadline and status == 'pass':
                status = 'failed'
            identifier = 'validation_' + secrets.token_hex(16)
            document = {**binding.public(), 'id': identifier, 'validationPlanDigest': self.definition_digest,
                        'status': status, 'checks': rows}
            receipt = TrustedValidationReceipt(identifier, canonical(document).decode('utf-8'), self._issuer)
            self._receipts[identifier] = receipt
        return receipt

    def require_pass(self, receipt, binding):
        with self._lock:
            _require(type(binding) is ValidationBinding
                and type(receipt) is TrustedValidationReceipt and receipt._issuer is self._issuer
                and self._receipts.get(receipt.receipt_id) is receipt, 'untrusted_validation_receipt')
            document = receipt.public()
            _require(all(document.get(key) == value for key, value in binding.public().items())
                     and document['validationPlanDigest'] == self.definition_digest, 'validation_binding')
            checks = document['checks']
            _require(not self._quarantined and document['status'] == 'pass'
                and {item['checkId'] for item in checks} == {item['id'] for item in self.plan['checks']}
                and all(item['status'] == 'pass' and item['terminationConfirmed'] is True
                        and item['cleanupConfirmed'] is True for item in checks), 'validation_failed')
            return document


__all__ = ['ValidationError', 'ValidationBinding', 'ValidationContext', 'ValidationObservation',
           'TrustedValidationAuthority', 'TrustedValidationReceipt']
