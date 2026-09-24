"""Project-authorized, bounded G9 repair jobs over the G7 issue workflow."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack
import hashlib
from pathlib import Path
import threading
import time

from .. import contracts
from ..contracts.versions import exact
from ..issue_package import PackageError
from ..project_repair import ProjectRepair, RepairError, RepairSource
from ..repair_journal import RepairJournal, RepairJournalError, TERMINAL
from .model import LiveError, check
from .evidence_store import EvidenceStoreError


@dataclass(frozen=True, slots=True)
class ProjectRepairConfiguration:
    source: RepairSource
    agent: object
    build_recipe_id: str
    validation_recipe_ids: tuple[str, ...]
    transfer_policy: object = None
    executor: object = None
    diagnostic_policy: object = None


class ProjectRepairJobs:
    def __init__(self, root, workflow, configurations, *, disk_limit=256 * 1024 * 1024, max_jobs=2):
        check(getattr(workflow, 'repairs', None) is None and not workflow._closed
              and type(max_jobs) is int and 1 <= max_jobs <= 4,
              'repair_configuration', 'Repair service is already configured or closed', 409)
        self.workflow = workflow
        self._lock = threading.RLock(); self._slots = threading.BoundedSemaphore(max_jobs)
        self._threads = {}; self._closed = False; self._stop = threading.Event()
        self.journal = None; self.runtimes = {}
        root = Path(root).absolute()
        check(type(disk_limit) is int and ProjectRepair.reservation_bytes <= disk_limit <= 512 * 1024 ** 3,
              'repair_configuration', 'Invalid repair storage reservation', 400)
        owner = 'project_repairs_' + hashlib.sha256(str(root).encode()).hexdigest()
        self._budget = workflow.lab._recording_budget.reserve(owner, 'transfer', disk_limit + 8 * 1024 * 1024,
                                                             idempotency_key=owner)
        self._budget.commit()
        try:
            self.journal = RepairJournal(root, disk_limit=disk_limit)
            for configured in configurations:
                check(type(configured) is ProjectRepairConfiguration and type(configured.source) is RepairSource,
                      'repair_configuration', 'A local repair source registration is required', 400)
                project_id = configured.source.project['id']
                runtime = workflow._runtime(project_id)
                check(project_id not in self.runtimes
                      and configured.source.project_digest == runtime.registration.project_digest
                      and tuple(sorted(configured.validation_recipe_ids)) == tuple(sorted(runtime.validation_recipe_ids)),
                      'repair_configuration', 'Repair registration differs from the issue project', 409)
                if configured.executor is not None:
                    from ..repair_verification import ProtectedRepairExecutor
                    check(type(configured.executor) is ProtectedRepairExecutor
                          and configured.executor.mobile.runner is runtime.service.runner
                          and configured.executor.mobile.adapter.runtime_policy_digest == contracts.digest(runtime.runtime_policy),
                          'repair_configuration', 'Protected repair uses a different scenario runtime', 409)
                self.runtimes[project_id] = ProjectRepair(configured.source, runtime.service.registry,
                    workflow.engines[project_id], self.journal, configured.agent,
                    build_recipe_id=configured.build_recipe_id,
                    validation_recipe_ids=configured.validation_recipe_ids, transfer_policy=configured.transfer_policy,
                    executor=configured.executor, diagnostic_policy=configured.diagnostic_policy)
            check(self.runtimes, 'repair_configuration', 'A repair project must be configured', 400)
            # Exclusive journal ownership makes these interrupted pins inert.
            for job in self.journal.list():
                workflow.lab._evidence_store.unpin_id(job['id'])
                workflow.packages.archive_evidence.unpin_id(job['id'])
            self.journal.apply_retention(now_ms=int(time.time() * 1000))
            workflow.repairs = self
            self._maintenance = threading.Thread(target=self._maintain, name='repro-project-repair-retention', daemon=False)
            self._maintenance.start()
        except Exception:
            if self.journal is not None: self.journal.close()
            raise

    def availability(self, project_id, principal=None):
        runtime = self.runtimes.get(project_id)
        result = runtime.availability() if runtime else {'proposalAvailable': False,
            'verificationAvailable': False, 'reason': 'repair_not_configured'}
        if result['verificationAvailable'] and principal is not None:
            try:
                for capability in ('project.maintain', 'replay.execute', 'fixture.execute'):
                    self.workflow._guard(principal, project_id, capability)()
                self.workflow.access.effect_authorizer(principal, project_id=project_id,
                    device_id=runtime.executor.device_id, project_digest=runtime.source.project_digest)()
            except contracts.ContractError:
                result.update(verificationAvailable=False, reason='verification_permission_required')
        return result

    @staticmethod
    def _public(job):
        return {key: value for key, value in job.items() if key not in {
            'request', 'requestDigest', 'reservedBytes', 'usedBytes', 'outputs'}}

    def _job(self, principal, identifier):
        self.workflow.access.authorize_resource(principal, 'job', identifier, 'job.read')
        job = self.journal.get(identifier)
        self.workflow._issue(principal, job['issueId'])
        check(job['projectId'] in self.runtimes, 'repair_not_configured', 'Repair project is not configured', 409)
        return job

    def _source(self, issue, recording_digest, principal_guard, diagnostic_digests=()):
        if issue['packageId']:
            with self.workflow.packages._lock:
                row = self.workflow.packages._row(issue['packageId'], issue['projectId'], principal_guard)
                return self.workflow.packages.archive_evidence, row['archive_digest'], row['expires_ms']
        evidence = self.workflow.lab._evidence_store
        try:
            references = [evidence.lookup(digest) for digest in (recording_digest, *diagnostic_digests)]
            check(all(reference is not None and reference.retain_until_ms > int(time.time() * 1000)
                for reference in references), 'repair_expired', 'Original evidence is no longer available', 410)
        except EvidenceStoreError:
            raise LiveError('repair_expired', 'Original evidence is no longer available', 410) from None
        return evidence, recording_digest, min(reference.retain_until_ms for reference in references)

    def start(self, principal, issue_id, body):
        exact(body, ('requestId', 'specificationDigest', 'mode'))
        contracts.validate_id(body['requestId']); contracts.validate_digest(body['specificationDigest'])
        check(type(body['mode']) is str and body['mode'] in {'propose', 'verify'}, 'repair_request', 'Invalid repair mode', 400)
        issue = self.workflow._issue(principal, issue_id, 'project.maintain')
        if body['mode'] == 'verify':
            self.workflow._authorize(principal, issue['projectId'], 'replay.execute')
            self.workflow._authorize(principal, issue['projectId'], 'fixture.execute')
        runtime = self.runtimes.get(issue['projectId'])
        check(runtime is not None, 'repair_not_configured', 'Repair proposals are not configured for this project', 409)
        view = self.workflow.get(principal, issue_id)
        check(view['approval'] is not None and view['specificationDigest'] == body['specificationDigest']
              and issue['state'] == 'reproduced' and issue['campaignId'] is not None,
              'baseline_unqualified', 'Approve and reproduce this exact issue revision before repair', 409)
        approved = self.workflow._approved(self.workflow._runtime(issue['projectId']),
            view['recording'], view['specification'], view['approval'])
        guards = [self.workflow._guard(principal, issue['projectId'], 'project.maintain')]
        if body['mode'] == 'verify':
            guards.extend(self.workflow._guard(principal, issue['projectId'], capability)
                          for capability in ('replay.execute', 'fixture.execute'))
            if runtime.executor is not None:
                guards.append(self.workflow.access.effect_authorizer(principal, project_id=issue['projectId'],
                    device_id=runtime.executor.device_id, project_digest=runtime.source.project_digest))
        def base_guard():
            for guard in guards: guard()
            return True
        base_guard()
        diagnostic_refs = []
        if runtime.diagnostic_policy is not None:
            from ..repair_diagnostics import diagnostic_references
            diagnostic_refs = diagnostic_references(approved.original)
        diagnostic_digests = tuple(dict.fromkeys(item['digest'] for item in diagnostic_refs))
        evidence, source_digest, source_expiry = self._source(issue, approved.recording_digest, base_guard, diagnostic_digests)
        policy = self.workflow._runtime(issue['projectId']).registration.collection_policy
        expiry = min(source_expiry, int(time.time() * 1000) + policy['retentionSeconds']['derivative'] * 1000)
        selected = tuple(issue[key] for key in ('specificationKey', 'approvalKey', 'campaignId'))
        def guard():
            base_guard()
            check(not self._closed, 'repair_closed', 'Repair service is closing', 409)
            current = self.workflow._get('issue', issue_id)
            check(tuple(current[key] for key in ('specificationKey', 'approvalKey', 'campaignId')) == selected
                  and current['state'] == 'reproduced' and int(time.time() * 1000) < expiry,
                  'repair_revision_changed', 'The approved issue revision or retention changed', 409)
            self._source(issue, approved.recording_digest, base_guard, diagnostic_digests)
            return True
        with self._lock:
            check(not self._closed, 'repair_closed', 'Repair service is closing', 409)
            existing = next((job for job in self.journal.list(project_id=issue['projectId'], owner_id=principal.principal_id)
                             if job['requestId'] == body['requestId']), None)
            acquired = False
            if existing is None:
                acquired = self._slots.acquire(blocking=False)
                check(acquired, 'repair_busy', 'Repair concurrency is full', 409)
            try:
                job = runtime.create(approved, campaign_id=issue['campaignId'], issue_id=issue_id,
                    owner_id=principal.principal_id, request_id=body['requestId'], mode=body['mode'], retain_until_ms=expiry)
                if existing is not None:
                    return {'repair': self._public(job)}
                self.workflow.access.bind_resource('job', job['id'], issue['projectId'], principal.principal_id, meaning='release')
                def run():
                    try:
                        with ExitStack() as pins:
                            pins.enter_context(evidence.pin(source_digest, job['id'], 'export'))
                            archived = {}
                            if diagnostic_refs:
                                if issue['packageId']:
                                    # Copy bounded inert objects, then release the
                                    # package lock before any provider can wait.
                                    with self.workflow.packages.open_archive(issue['packageId'], issue['projectId'],
                                            authorize=base_guard) as archive:
                                        for reference in diagnostic_refs:
                                            guard()
                                            item = archive.object(reference['digest'], limits=self.workflow.packages.limits)
                                            check(item['mimeType'] == reference['mimeType']
                                                and len(item['body']) == reference['bytes'],
                                                'diagnostics_binding', 'Diagnostic source changed', 409)
                                            archived[reference['digest']] = item['body']
                                else:
                                    for digest in diagnostic_digests:
                                        pins.enter_context(evidence.pin(digest, job['id'], 'export'))
                            def read_diagnostic(reference):
                                guard()
                                check(reference in diagnostic_refs, 'diagnostics_binding', 'Diagnostic source is not in the recording', 409)
                                if issue['packageId']:
                                    raw = archived[reference['digest']]
                                else:
                                    stored = evidence.lookup(reference['digest'])
                                    check(stored is not None and stored.bytes == reference['bytes'],
                                          'diagnostics_binding', 'Diagnostic source size changed', 409)
                                    raw = evidence.read(reference['digest'])
                                guard()
                                return raw
                            runtime.execute(job['id'], approved, authorize=guard,
                                diagnostic_reader=read_diagnostic if diagnostic_refs else None)
                    except Exception:
                        # Fixed metadata only; never persist provider exception text.
                        current = self.journal.get(job['id'])
                        if current['status'] not in TERMINAL:
                            self.journal.finish(job['id'], 'cancelled' if current['cancelRequested'] else 'failed',
                                                reason='repair_unavailable', result={'verified': False})
                    finally:
                        with self._lock:
                            self._threads.pop(job['id'], None)
                            self._slots.release()
                thread = threading.Thread(target=run, name='repro-project-repair', daemon=False)
                self._threads[job['id']] = thread
                try:
                    thread.start()
                except RuntimeError:
                    self._threads.pop(job['id'], None)
                    self.journal.finish(job['id'], 'failed', reason='repair_unavailable', result={'verified': False})
                    raise LiveError('repair_unavailable', 'Proposal worker could not start', 503) from None
                acquired = False
                return {'repair': self._public(job)}
            finally:
                if acquired: self._slots.release()

    def list(self, principal, issue_id):
        issue = self.workflow._issue(principal, issue_id)
        return {'availability': self.availability(issue['projectId'], principal), 'repairs': [self._public(job)
            for job in self.journal.list(project_id=issue['projectId']) if job['issueId'] == issue_id]}

    def get(self, principal, identifier):
        return {'repair': self._public(self._job(principal, identifier))}

    def cancel(self, principal, identifier):
        job = self._job(principal, identifier)
        if job['ownerId'] != principal.principal_id:
            self.workflow._authorize(principal, job['projectId'], 'project.maintain')
        return {'repair': self._public(self.journal.cancel(identifier))}

    def proposal(self, principal, identifier):
        return self._output(principal, identifier, 'proposal')

    def diagnostics(self, principal, identifier):
        return self._output(principal, identifier, 'diagnostics')

    def _output(self, principal, identifier, kind):
        with self._lock:
            job = self._job(principal, identifier)
            self.workflow._authorize(principal, job['projectId'], 'project.maintain')
            check(not job['outputsExpired'] and (job['retainUntilMs'] is None
                  or job['retainUntilMs'] > int(time.time() * 1000)),
                  'repair_expired', 'Repair source output has expired', 410)
            check(job['plan'] is not None, 'repair_unavailable', 'Repair evidence is not ready', 409)
            view = self.workflow.get(principal, job['issueId'])
            guard = self.workflow._guard(principal, job['projectId'], 'project.maintain')
            self._source(view['issue'], job['plan']['recordingDigest'], guard,
                         job['plan'].get('diagnosticSourceDigests', ()))
            result = getattr(self.runtimes[job['projectId']], kind)(identifier)
            self._source(view['issue'], job['plan']['recordingDigest'], guard,
                         job['plan'].get('diagnosticSourceDigests', ()))
            self.workflow._guard(principal, job['projectId'], 'project.maintain')()
            return result

    def _maintain(self):
        while not self._stop.wait(5):
            try:
                self.apply_retention()
            except (RepairJournalError, contracts.ContractError, OSError):
                # Unconfirmed deletion retains its charge; reads enforce expiry.
                continue

    def apply_retention(self):
        with self._lock:
            now = int(time.time() * 1000)
            for job in self.journal.list():
                if job.get('outputsExpired') or not job['plan']: continue
                try:
                    issue = self.workflow._get('issue', job['issueId'])
                    self._source(issue, job['plan']['recordingDigest'], lambda: True,
                                 job['plan'].get('diagnosticSourceDigests', ()))
                except (LiveError, PackageError):
                    self.journal.expire(job['id'], now_ms=now)
            self.journal.apply_retention(now_ms=now)

    def close(self):
        with self._lock:
            self._closed = True; self._stop.set()
            if self.journal is None: return
            for job in self.journal.list():
                if job['status'] not in TERMINAL: self.journal.cancel(job['id'])
            threads = list(self._threads.values())
        self._maintenance.join(2)
        deadline = time.monotonic() + 10
        for thread in threads: thread.join(max(0, deadline-time.monotonic()))
        check(not any(thread.is_alive() for thread in threads), 'repair_busy', 'Repair cancellation is still running', 409)
        with self._lock:
            self.journal.apply_retention(now_ms=int(time.time() * 1000))
            self.journal.close(); self.journal = None


__all__ = ['ProjectRepairConfiguration', 'ProjectRepairJobs']
