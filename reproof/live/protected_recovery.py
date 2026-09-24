"""Authenticated, bounded mobile recovery through existing registered runtimes."""
from contextlib import contextmanager
import copy
import threading
import time
import uuid

from .. import contracts
from ..execution.journal import RunStore
from ..protected_mobile_inputs import load_protected_mobile_inputs
from ..repair_android_operation import AndroidOperationStore
from .access import AccessError
from .authority import DeviceAuthority, HostAuthority
from .model import LiveError, check


_TERMINAL = frozenset(('succeeded', 'failed', 'cancelled'))


def _identifier(value):
    try:
        contracts.validate_id(value)
    except contracts.ContractError:
        raise LiveError('invalid_recovery_request', 'A valid recovery identifier is required', 400) from None
    return value


def compose_recovery_workflow(lab, access, configuration, issue_configuration, *, root,
                              video_helper=None, media_helper=None):
    from .issue_configuration import compose_issue_workflow
    bundle = compose_issue_workflow(lab, access, issue_configuration, root=root,
        video_helper=video_helper, media_helper=media_helper, defer_repairs=True)
    try:
        bundle.protected_recovery = ProtectedRecoveryService(configuration, issue_configuration, bundle)
        return bundle
    except Exception:
        bundle.close()
        raise


class _AuthorizationCancellation:
    def __init__(self, service, principal, profile, event):
        self.service, self.principal, self.profile, self.event = service, principal, profile, event
        self.revoked = False

    def is_set(self):
        if self.event.is_set() or self.service._stopped.is_set():
            return True
        try:
            self.service._authorize(self.principal, self.profile, operate=True)
        except (LiveError, contracts.ContractError):
            self.revoked = True
            return True
        return False


class ProtectedRecoveryService:
    def __init__(self, configuration, issue_configuration, runtime_bundle):
        self.configuration = configuration
        self.issue_configuration = copy.deepcopy(issue_configuration)
        self.bundle = runtime_bundle
        self.inputs = load_protected_mobile_inputs(configuration, self.issue_configuration, runtime_bundle)
        self.lab, self.access = runtime_bundle.workflow.lab, runtime_bundle.workflow.access
        check(type(self.lab.authority) is HostAuthority and callable(self.lab.project_grant_provider),
              'recovery_authority_unavailable', 'Recovery needs the current host and project grant provider', 409)
        self._profiles = {row['id']: row for row in configuration.document['profiles']}
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._jobs = {}
        self._requests = {}

    def _profile(self, identifier):
        _identifier(identifier)
        check(identifier in self._profiles, 'recovery_not_configured', 'Recovery profile is not configured', 404)
        return self.inputs.profile(identifier)

    def _authorize(self, principal, profile, *, operate=False):
        try:
            self.access.store.current_principal(principal)
            current = self.access.operation_principal(principal.credential_id, principal.authorization_id)
            check(current.principal_id == principal.principal_id, 'authorization_revoked', 'Authorization changed', 403)
            config = profile.config
            registration = self.access.registration(config.registration.project['id'])
            check(registration.project_digest == config.registration.project_digest,
                  'stale_project', 'Recovery project revision changed', 409)
            self.access.authorize_device(current, config.device_id, registration.project['id'],
                                         'device.operate' if operate else 'device.read')
            if operate:
                self.access.store.authorize(current, registration.project['id'], 'fixture.execute')
            return current
        except AccessError as error:
            raise LiveError(error.code, str(error), error.status) from None

    @contextmanager
    def _operations(self, profile):
        self.inputs.verify(self.configuration, self.issue_configuration, self.bundle)
        selected = self._profiles[profile.profile_id]['mobile']
        journal = selected['journal']
        store = RunStore(journal['root'], environment_digest=journal['environmentDigest'],
                         disk_limit=journal['diskBudgetBytes'], create=False)
        check(store._load().get('scope') == {'kind': 'mobile-device', 'scopeDigest': profile.config.scope_digest},
              'recovery_binding', 'The original mobile journal is required', 409)
        from ..ios_mobile_inputs import LoadedIOSMobileInputs
        from ..ios_mobile_operation import IOSMobileOperationStore
        operations = (IOSMobileOperationStore(store, profile.config.definition, selected['ownerRoot'], create=False)
            if type(profile) is LoadedIOSMobileInputs else
            AndroidOperationStore(store, profile.config, selected['ownerRoot'], create=False))
        try:
            yield operations
        finally:
            check(operations.close(deadline_monotonic=time.monotonic()+5),
                  'recovery_owner_busy', 'The native recovery owner has not been collected', 409)

    def profiles(self, principal):
        visible = []
        for identifier in self._profiles:
            profile = self._profile(identifier)
            try:
                self._authorize(principal, profile)
            except LiveError:
                continue
            visible.append(profile.public())
        return {'profiles': visible}

    def status(self, principal, profile_id, operation_id):
        profile = self._profile(profile_id); _identifier(operation_id)
        self._authorize(principal, profile)
        try:
            with self._operations(profile) as operations:
                row = operations.run_store.status(operation_id)
                observed = operations.status(operation_id)
                check(observed.get('configurationDigest') == operations.configuration_digest
                    and observed.get('requestDigest') == row['requestDigest'],
                    'recovery_operation_unavailable', 'The original operation does not match this profile', 409)
                self._authorize(principal, profile)
                return {'profileId': profile_id, 'operation': observed,
                        'runState': row['state'], 'reservedBytes': row['reservedBytes']}
        except LiveError:
            raise
        except (RuntimeError, OSError, contracts.ContractError):
            raise LiveError('recovery_operation_unavailable', 'The original recovery journal or inputs are unavailable', 409) from None

    def operations(self, principal, profile_id):
        profile = self._profile(profile_id)
        self._authorize(principal, profile)
        try:
            with self._operations(profile) as operations:
                values = []
                for identity, row in operations.run_store._load()['runs'].items():
                    observed = operations.status(identity)
                    if (observed.get('configurationDigest') == operations.configuration_digest
                            and observed.get('requestDigest') == row['requestDigest']):
                        values.append({'operationId': identity, 'requestDigest': row['requestDigest'],
                            'runState': row['state'], 'reservedBytes': row['reservedBytes'],
                            'state': observed.get('state', observed.get('runState'))})
                self._authorize(principal, profile)
                return {'profileId': profile_id, 'operations': values}
        except LiveError:
            raise
        except (RuntimeError, OSError, contracts.ContractError):
            raise LiveError('recovery_operation_unavailable', 'The original recovery journal or inputs are unavailable', 409) from None

    def configured_device(self, device_id):
        return any(row.config.device_id == device_id for row in self.inputs._profiles)

    @staticmethod
    def _public(job):
        return copy.deepcopy({key: job[key] for key in
            ('id', 'profileId', 'operationId', 'requestDigest', 'requestId', 'state', 'result', 'error')})

    def start(self, principal, *, profile_id, operation_id, request_digest, request_id, timeout_seconds=120):
        profile = self._profile(profile_id)
        for value in (operation_id, request_id): _identifier(value)
        try:
            contracts.validate_digest(request_digest)
        except contracts.ContractError:
            raise LiveError('invalid_recovery_request', 'The original request digest is required', 400) from None
        check(type(timeout_seconds) is int and 10 <= timeout_seconds <= 600,
              'invalid_recovery_timeout', 'Recovery timeout must be 10–600 seconds', 400)
        self._authorize(principal, profile, operate=True)
        key = (principal.principal_id, profile_id, request_id)
        fingerprint = contracts.digest({'profileId': profile_id, 'operationId': operation_id,
            'requestDigest': request_digest, 'timeoutSeconds': timeout_seconds})
        with self._lock:
            check(not self._stopped.is_set(), 'recovery_closed', 'Recovery service is stopping', 409)
            previous = self._requests.get(key)
            if previous is not None:
                job = self._jobs[previous]
                check(job['_fingerprint'] == fingerprint, 'recovery_request_conflict', 'Recovery request identity changed', 409)
                return self._public(job)
            if len(self._jobs) >= 128:
                expired = next((identity for identity, item in self._jobs.items() if item['state'] in _TERMINAL), None)
                check(expired is not None, 'recovery_busy', 'Recovery job limit reached', 409)
                removed = self._jobs.pop(expired); self._requests.pop(removed['_key'], None)
            check(not any(item['_deviceId'] == profile.config.device_id and item['state'] not in _TERMINAL
                          for item in self._jobs.values()), 'recovery_busy', 'Device recovery is already running', 409)
            check(sum(item['state'] not in _TERMINAL for item in self._jobs.values()) < 4,
                  'recovery_busy', 'Recovery execution limit reached', 409)
            identity = 'recovery_' + uuid.uuid4().hex
            job = {'id': identity, 'profileId': profile_id, 'operationId': operation_id,
                'requestDigest': request_digest, 'requestId': request_id, 'state': 'queued', 'result': None, 'error': None,
                '_key': key, '_fingerprint': fingerprint, '_deviceId': profile.config.device_id,
                '_cancel': threading.Event()}
            job['_thread'] = threading.Thread(target=self._run, args=(job, principal, profile, timeout_seconds),
                                               name='protected-mobile-recovery', daemon=True)
            self._jobs[identity] = job; self._requests[key] = identity
            job['_thread'].start()
            return self._public(job)

    def job(self, principal, identifier):
        _identifier(identifier)
        with self._lock:
            check(identifier in self._jobs, 'recovery_job_not_found', 'Recovery job is unavailable', 404)
            job = self._jobs[identifier]
            self._authorize(principal, self._profile(job['profileId']))
            return self._public(job)

    def cancel(self, principal, identifier):
        _identifier(identifier)
        with self._lock:
            check(identifier in self._jobs, 'recovery_job_not_found', 'Recovery job is unavailable', 404)
            job = self._jobs[identifier]
            self._authorize(principal, self._profile(job['profileId']), operate=True)
            job['_cancel'].set()
            return self._public(job)

    def _mark_device(self, job, config):
        with self.lab.lock:
            device = self.lab.devices[config.device_id]
            check(device['state'] in ('available', 'reserved', 'quarantined'),
                  'device_busy', 'Close the active device owner before recovery', 409)
            check(not any(session['deviceId'] == config.device_id and session.get('state') not in ('closed', 'failed')
                          for session in self.lab.sessions.values()), 'device_busy', 'A device session is still active', 409)
            retained = [reservation for reservation in self.lab._device_reservations.values()
                        if reservation.device_id == config.device_id]
            for reservation in retained:
                handle = reservation._authority_handle
                check(type(handle) is DeviceAuthority and handle._closed
                    and reservation.owner == config.owner and reservation.project_digest == config.registration.project_digest
                    and reservation.application_id == config.application_id and reservation.build_id == config.original_build_id,
                    'device_busy', 'The original retained device owner must stop before recovery', 409)
            check(device['sessionId'] is None or any(reservation.reservation_id == device['sessionId'] for reservation in retained),
                  'device_busy', 'Device ownership is still active', 409)
            prior_session = device['sessionId']
            device.update(state='recovering', sessionId=job['id'])
            try:
                self.lab._persist_devices()
            except Exception:
                device.update(state='quarantined', sessionId=prior_session)
                raise
            return retained, prior_session

    def _finish_device(self, job, config, retained, prior_session, released):
        with self.lab.lock:
            device = self.lab.devices[config.device_id]
            check(device['state'] == 'recovering' and device['sessionId'] == job['id'],
                  'recovery_binding', 'Recovery device ownership changed', 409)
            if released:
                for reservation in retained:
                    if self.lab._device_reservations.get(reservation.reservation_id) is reservation:
                        self.lab._device_reservations.pop(reservation.reservation_id)
                        self.lab._retained_device_scopes.pop(reservation.reservation_id, None)
                        self.lab._retained_scope_sequences.pop(reservation.reservation_id, None)
                device.update(state='available', sessionId=None)
                device.pop('quarantineReason', None)
            else:
                device.update(state='quarantined', sessionId=prior_session, quarantineReason='protected_recovery_unconfirmed')
            self.lab._persist_devices()

    def _run(self, job, principal, profile, timeout_seconds):
        deadline = time.monotonic()+timeout_seconds
        cancellation = _AuthorizationCancellation(self, principal, profile, job['_cancel'])
        config = profile.config
        handle = marked = None
        released = False
        result = None
        error = None
        try:
            with self._lock: job['state'] = 'running'
            check(not cancellation.is_set(), 'authorization_revoked', 'Recovery authorization ended', 403)
            with self._operations(profile) as operations:
                row = operations.run_store.status(job['operationId'])
                check(row['requestDigest'] == job['requestDigest'], 'recovery_binding', 'The original request changed', 409)
                marked = self._mark_device(job, config)
                grant = self.lab.project_grant_provider(config.registration)
                check(grant.project_id == config.registration.project['id'], 'recovery_binding', 'Recovery project grant changed', 409)
                self.lab.authority._require_parent_grant(grant)
                check(not cancellation.is_set(), 'authorization_revoked', 'Recovery authorization ended', 403)
                from ..ios_mobile_inputs import IOSMobileInputsConfig
                ios = type(config) is IOSMobileInputsConfig
                handle = self.lab.authority.claim_device(device_kind='ios-physical' if ios else 'android',
                    physical_id=config.query.udid if ios else config.serial,
                    helper_incarnation='helper_recovery_' + uuid.uuid4().hex, parent_grant=grant)
                observation = operations.finalize_recovery(job['operationId'], job['requestDigest'], device=handle,
                    parent_grant=grant, cancellation=cancellation, deadline_monotonic=deadline,
                    **({'config': config} if ios else {}))
                released = handle.close()
                row = operations.run_store.status(job['operationId'])
                check(observation.ownership_released and observation.reservation_released and released and row['reservedBytes'] == 0,
                      'recovery_unconfirmed', 'Recovery ownership release is unconfirmed', 409)
                result = {'operationId': job['operationId'], 'runState': row['state'], 'reservedBytes': row['reservedBytes'],
                    'ownershipReleased': True, 'evidenceDigest': observation.evidence_digest}
        except Exception:
            error = {'code': 'authorization_revoked' if cancellation.revoked else 'protected_recovery_unconfirmed',
                     'message': 'Recovery was not confirmed; inspect the original operation and device state'}
        finally:
            if handle is not None and not handle._closed:
                try:
                    handle.close()
                except Exception:
                    released = False; result = None
                    error = {'code': 'recovery_owner_unconfirmed', 'message': 'Device ownership remains unconfirmed'}
            if marked is not None:
                try:
                    self._finish_device(job, config, *marked, released and result is not None)
                except Exception:
                    result = None
                    error = {'code': 'recovery_device_state', 'message': 'Device state publication needs recovery'}
            with self._lock:
                job['result'], job['error'] = result, error
                job['state'] = 'succeeded' if result is not None else (
                    'cancelled' if job['_cancel'].is_set() or self._stopped.is_set() else 'failed')

    def close(self, *, deadline_monotonic=None):
        deadline = time.monotonic()+10 if deadline_monotonic is None else deadline_monotonic
        self._stopped.set()
        with self._lock:
            threads = [job['_thread'] for job in self._jobs.values()]
            for job in self._jobs.values(): job['_cancel'].set()
        for thread in threads:
            thread.join(max(0, deadline-time.monotonic()))
        return all(not thread.is_alive() for thread in threads)


def dispatch(handler, principal, route):
    service = handler.server.protected_recovery
    check(service is not None, 'recovery_not_configured', 'Protected recovery is not configured', 409)
    method = handler.command
    if route == ['profiles'] and method == 'GET':
        return handler.respond(200, service.profiles(principal))
    if len(route) == 3 and route[0] == 'profiles' and route[2] == 'operations' and method == 'GET':
        return handler.respond(200, service.operations(principal, route[1]))
    if len(route) in (4, 5) and route[0] == 'profiles' and route[2] == 'operations':
        if len(route) == 4 and method == 'GET':
            return handler.respond(200, service.status(principal, route[1], route[3]))
        if len(route) == 5 and route[4] == 'recover' and method == 'POST':
            body = handler.body()
            check(set(body) == {'requestDigest', 'requestId', 'timeoutSeconds'},
                  'invalid_recovery_request', 'Expected the original request, request identity and timeout', 400)
            job = service.start(principal, profile_id=route[1], operation_id=route[3],
                request_digest=body['requestDigest'], request_id=body['requestId'], timeout_seconds=body['timeoutSeconds'])
            return handler.respond(202, {'job': job})
    if len(route) == 2 and route[0] == 'jobs' and method == 'GET':
        return handler.respond(200, {'job': service.job(principal, route[1])})
    if len(route) == 3 and route[0] == 'jobs' and route[2] == 'cancel' and method == 'POST':
        check(not handler.body(), 'invalid_recovery_request', 'Recovery cancellation body must be empty', 400)
        return handler.respond(200, {'job': service.cancel(principal, route[1])})
    raise LiveError('not_found', 'Protected recovery route is unavailable', 404)
