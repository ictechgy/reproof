"""Authenticated asynchronous repair proposals on real G4/G7 loopback services."""
import http.client
import json
import threading
import time
import unittest
from unittest import mock

from reproof.live.server import LiveServer
from tests.g9_support import RepairEnvironment
from tests.test_project_repair_jobs import LocalProposalDouble
from tests import test_issue_workflow as workflow_support


class LiveProjectRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_support.IssueWorkflowTests('runTest')
        with mock.patch('tests.test_issue_workflow.G4Environment', RepairEnvironment):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.workflow, self.env = self.fixture.workflow, self.fixture.env
        self.issue_id, view = self.fixture.recorded()
        saved = self.fixture.save(self.issue_id, view['recording'])
        self.fixture.approve(self.issue_id, saved)
        self.spec_digest = saved['specificationDigest']
        self.workflow.replay(self.fixture.owner, self.issue_id, {'deviceId': 'device',
            'clientId': 'baseline', 'specificationDigest': self.spec_digest})
        self.fixture.wait(self.issue_id, states={'reproduced'})

    def service(self, agent=None, executor=None, disk_limit=256 * 1024 * 1024):
        from reproof.live.repair_jobs import ProjectRepairConfiguration, ProjectRepairJobs
        self.agent = agent or LocalProposalDouble()
        self.repairs = ProjectRepairJobs(self.env.root / 'project-repairs', self.workflow,
            (ProjectRepairConfiguration(self.env.source, self.agent, 'build_app', ('regression_ui',), executor=executor),),
            disk_limit=disk_limit)
        return self.repairs

    def protected_runtime(self):
        from tests.g9_execution_support import SyntheticRepairExecution
        self.protected = SyntheticRepairExecution(self.env)
        self.addCleanup(self.protected.close)
        return self.protected.executor()

    def test_registered_executor_runs_verified_job_and_enforces_device_roles(self):
        from reproof.live.access import AccessError
        self.service(executor=self.protected_runtime())
        self.assertTrue(self.workflow.projects(self.fixture.owner)['projects'][0]['repair']['verificationAvailable'])
        self.assertFalse(self.workflow.projects(self.fixture.principals['viewer'])['projects'][0]['repair']['verificationAvailable'])
        with self.assertRaises(AccessError): self.start(mode='verify', owner=self.fixture.principals['maintainer'])
        started = self.start(mode='verify'); completed = self.wait(started['id'])
        self.assertEqual((completed['status'], completed['result']['verified']), ('verified', True), completed['reason'])
        self.assertEqual(len(completed['result']['afterEvidence']['attempts']), 3)
        self.assertIn('if ready', self.repairs.proposal(self.fixture.owner, started['id'])['patch'])

    def test_availability_observer_does_not_take_execution_leases(self):
        self.service(executor=self.protected_runtime())
        runtime = self.repairs.runtimes['checkout']; executor = runtime.executor
        targets = ((executor.builder.backend.store, lambda: executor.builder.ready(
            project_digest=self.env.registration.project_digest, recipe_id='build_app',
            validation_plan_digest=self.protected.plan.definition_digest)),
            (executor.signer.store, executor.signer.ready),
            (executor.mobile.store, lambda: executor.mobile.ready(project_digest=self.env.registration.project_digest,
                runtime_policy_digest=executor.mobile.adapter.runtime_policy_digest,
                validation_recipe_ids=runtime.validation_recipe_ids)))
        for store, execute_ready in targets:
            with self.subTest(store=store.root.name):
                entered, release = threading.Event(), threading.Event()
                original = store.require_available; observed = {}
                def held():
                    result = original()
                    if threading.current_thread().name == 'owned-availability-observer':
                        entered.set(); release.wait(3)
                    return result
                def observe(): observed.update(runtime.availability())
                with mock.patch.object(store, 'require_available', side_effect=held):
                    thread = threading.Thread(target=observe, name='owned-availability-observer'); thread.start()
                    try:
                        self.assertTrue(entered.wait(2)); execute_ready()
                    finally:
                        release.set(); thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertTrue(observed['verificationAvailable'])

    def test_operator_permission_revocation_after_proposal_prevents_protected_dispatch(self):
        from tests.test_execution_runtime import VMDouble
        executor = self.protected_runtime()
        def revoke(_):
            self.fixture.access_store.revoke_membership('admin', 'checkout', 'owner', 'operator')
        self.service(LocalProposalDouble(effect=revoke), executor=executor)
        completed = self.wait(self.start(mode='verify')['id'])
        self.assertEqual(completed['status'], 'cancelled')
        self.assertTrue(completed['cancelRequested'])
        self.assertEqual(VMDouble.instances, [])
        self.assertEqual(self.protected.installs, [])

    def start(self, *, mode='propose', request='request', owner=None):
        return self.repairs.start(owner or self.fixture.owner, self.issue_id,
            {'requestId': request, 'specificationDigest': self.spec_digest, 'mode': mode})['repair']

    def wait(self, identifier):
        from reproof.repair_journal import TERMINAL
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.repairs.journal.get(identifier)
            if result['status'] in TERMINAL: return result
            time.sleep(.01)
        self.fail('Repair job did not finish')

    def test_roles_separate_issue_status_from_product_source_and_execution(self):
        from reproof.live.access import AccessError
        self.service()
        viewer = self.fixture.principals['viewer']
        with self.assertRaises(AccessError): self.start(owner=viewer)
        job = self.start(); self.assertEqual(self.wait(job['id'])['status'], 'proposal-ready')
        self.assertEqual(self.repairs.list(viewer, self.issue_id)['repairs'][0]['id'], job['id'])
        with self.assertRaises(AccessError): self.repairs.proposal(viewer, job['id'])
        result = self.repairs.proposal(self.fixture.owner, job['id'])
        self.assertFalse(result['verified'])
        self.assertIn('if ready', result['patch'])
        self.assertNotIn(str(self.env.root), json.dumps(self.repairs.get(viewer, job['id'])))
        self.assertFalse(self.workflow.projects(viewer)['projects'][0]['repair']['verificationAvailable'])

    def test_real_http_routes_reject_forged_results_and_run_proposal(self):
        self.service()
        store = self.fixture.access_store
        token = store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
        server = LiveServer(self.env.lab, access=self.fixture.access, issue_workflow=self.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close(): server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(close)
        def call(path, body=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('POST' if body is not None else 'GET', path,
                    body=None if body is None else json.dumps(body), headers={'Authorization': 'Bearer ' + token,
                        'Origin': server.origin, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally: connection.close()
        path = '/api/release/issues/' + self.issue_id + '/repairs'
        request = {'requestId': 'http_request', 'specificationDigest': self.spec_digest, 'mode': 'propose'}
        self.assertEqual(call(path, dict(request, verified=True))[0], 400)
        status, result = call(path, request); self.assertEqual(status, 202)
        job = self.wait(result['repair']['id']); self.assertEqual(job['status'], 'proposal-ready')
        self.assertEqual(call(path, request)[1]['repair']['id'], job['id'])
        self.assertEqual(self.agent.calls, 1)
        self.assertEqual(call('/api/release/repairs/' + job['id'] + '/proposal')[0], 200)
        status, result = call(path, dict(request, requestId='verify', mode='verify'))
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(result['repair']['id'])['status'], 'blocked')
        self.assertEqual(self.agent.calls, 1)

    def test_cancellation_and_revoked_authority_stop_inflight_proposal(self):
        entered = threading.Event()
        def wait_cancel(cancel): entered.set(); cancel.wait(3)
        self.service(LocalProposalDouble(effect=wait_cancel))
        job = self.start(); self.assertTrue(entered.wait(2))
        self.repairs.cancel(self.fixture.owner, job['id'])
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        entered.clear()
        job = self.start(request='revoke'); self.assertTrue(entered.wait(2))
        self.fixture.access_store.revoke_membership('admin', 'checkout', 'owner', 'maintainer')
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        self.assertEqual(self.repairs.journal.get(job['id'])['outputs'], {})

    def test_expired_derivative_is_deleted_and_cannot_be_read(self):
        from reproof.live.model import LiveError
        self.service(); job = self.start(); result = self.wait(job['id'])
        self.assertIsInstance(result['retainUntilMs'], int)
        self.repairs.journal.apply_retention(now_ms=result['retainUntilMs'] + 1)
        with self.assertRaises(LiveError) as caught:
            self.repairs.proposal(self.fixture.owner, job['id'])
        self.assertEqual(caught.exception.status, 410)
        self.assertFalse((self.repairs.journal.root / job['id'] / 'candidate').exists())

    def test_removed_original_propagates_to_proposal_retention(self):
        self.service(); job = self.start(); result = self.wait(job['id'])
        deadline = time.monotonic() + 2
        while self.repairs._threads and time.monotonic() < deadline: time.sleep(.01)
        self.env.lab._evidence_store.tombstone(result['plan']['recordingDigest'], reason='operator_removed')
        self.repairs.apply_retention()
        current = self.repairs.journal.get(job['id'])
        self.assertTrue(current['outputsExpired'])
        self.assertFalse((self.repairs.journal.root / job['id'] / 'candidate').exists())

    def test_failed_thread_start_retains_failure_and_releases_the_slot(self):
        from reproof.live.model import LiveError
        self.service()
        with mock.patch('reproof.live.project_repair_jobs.threading.Thread.start', side_effect=RuntimeError('owned failure')):
            with self.assertRaises(LiveError): self.start()
        job = self.repairs.journal.list()[0]
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.repairs._threads, {})
        self.assertEqual(self.wait(self.start(request='recovered')['id'])['status'], 'proposal-ready')


if __name__ == '__main__': unittest.main()
