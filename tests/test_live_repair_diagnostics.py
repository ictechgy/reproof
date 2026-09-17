"""Real issue stores bind synthetic provider logs to general repair proposals."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from reproloop import contracts
from reproloop.live.model import LiveError
from tests.g4_support import ScenarioProvider, SECRET
from tests.g9_support import RepairEnvironment
from tests.test_project_repair import source_fixture, PRODUCT
from tests.test_project_repair_jobs import LocalProposalDouble
from tests.test_repair_diagnostics import APP_LOG_MIME, app_log, diagnostic_policy
from tests import test_issue_workflow as workflow_support


class LoggingScenarioProvider(ScenarioProvider):
    def render(self):
        from tests.test_issue_media import png
        self.lab.publish_frame(self.session['id'], png(8, 12), 'image/png', 8, 12, 'portrait')

    def collect_app_logs(self):
        return copy.deepcopy(self.control.get('app_log', app_log()))


class LiveRepairDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        project, sources, artifacts = source_fixture()
        project['evidencePolicy'].update(logs=True, aiEligible=True)
        self.fixture = workflow_support.IssueWorkflowTests('runTest')
        with mock.patch('tests.test_issue_workflow.G4Environment', RepairEnvironment), \
             mock.patch('tests.g9_support.source_fixture', return_value=(project, sources, artifacts)):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.workflow, self.env = self.fixture.workflow, self.fixture.env
        self.env.lab.devices['device']['factory'] = lambda: LoggingScenarioProvider(self.env.control)
        self.env.lab.devices['device']['capabilities']['automaticAppLogs'] = True
        self.issue_id, self.view = self.recorded_with_logs()
        saved = self.fixture.save(self.issue_id, self.view['recording'])
        self.fixture.approve(self.issue_id, saved)
        self.spec_digest = saved['specificationDigest']
        self.workflow.replay(self.fixture.owner, self.issue_id, {'deviceId': 'device',
            'clientId': 'baseline', 'specificationDigest': self.spec_digest})
        self.fixture.wait(self.issue_id, states={'reproduced'})

    def recorded_with_logs(self):
        issue_id = self.fixture.start()
        active = self.fixture.wait(issue_id, states={'recording'})
        session = self.env.lab.get_session(active['issue']['sessionId'], 'owner')
        for sequence, action in enumerate((
            {'action': 'tap', 'parameters': {}, 'target': {'kind': 'accessibility-id', 'value': 'checkout'}},
            {'action': 'text', 'parameters': {'variableId': 'secret_text'},
             'target': {'kind': 'accessibility-id', 'value': 'account'}},
        ), 1):
            self.workflow.input(self.fixture.owner, issue_id, {'input': action, 'operationId': f'manual_{sequence}',
                'sequence': sequence, 'controllerId': session['controllerId'], 'epoch': session['epoch']})
        self.env.lab.app_logs(session['id'], 'owner')
        self.workflow.stop(self.fixture.owner, issue_id)
        return issue_id, self.fixture.wait(issue_id, states={'complete', 'failed', 'quarantined'})

    def service(self, agent=None, transfer_policy=None):
        from reproloop.live.project_repair_jobs import ProjectRepairConfiguration, ProjectRepairJobs
        self.agent = agent or LocalProposalDouble()
        self.repairs = ProjectRepairJobs(self.env.root / 'diagnostic-repairs', self.workflow,
            (ProjectRepairConfiguration(self.env.source, self.agent, 'build_app', ('regression_ui',),
                transfer_policy=transfer_policy, diagnostic_policy=diagnostic_policy(self.env.project)),))
        return self.repairs

    def start(self, request='request'):
        return self.repairs.start(self.fixture.owner, self.issue_id, {'requestId': request,
            'specificationDigest': self.spec_digest, 'mode': 'propose'})['repair']

    def wait(self, identifier):
        from reproloop.repair_journal import TERMINAL
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.repairs.journal.get(identifier)
            if job['status'] in TERMINAL: return job
            time.sleep(.01)
        self.fail('Diagnostic repair did not finish')

    def references(self):
        return [item for item in self.view['recording']['original']['observations'] if item['mimeType'] == APP_LOG_MIME]

    def test_release_logs_use_retained_typed_objects_and_closed_reads_honor_removal(self):
        references = self.references()
        self.assertTrue(references)
        sid = self.view['issue']['sessionId']
        self.assertFalse((self.env.lab.output / 'app-logs' / sid).exists())
        self.assertEqual(self.env.lab.app_logs(sid, 'owner'), app_log())
        self.env.lab._evidence_store.tombstone(references[-1]['digest'], reason='operator_removed')
        with self.assertRaises(LiveError) as caught: self.env.lab.app_logs(sid, 'owner')
        self.assertEqual(caught.exception.status, 410)

    def test_expired_closed_log_is_removed_from_memory_without_a_download(self):
        sid = self.view['issue']['sessionId']
        reference = self.env.lab._evidence_store.lookup(self.references()[0]['digest'])
        with mock.patch('reproloop.live.model.time.time', return_value=reference.retain_until_ms / 1000 + 1):
            self.env.lab.reap_expired()
        self.assertFalse('appLog' in self.env.lab._session(sid, 'owner'))

    def test_proposal_stores_the_actual_derived_packet_and_immutable_source_binding(self):
        self.service(); result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'proposal-ready', result['reason'])
        diagnostic = self.repairs.diagnostics(self.fixture.owner, result['id'])
        self.assertEqual(diagnostic, self.agent.packet['diagnostics'])
        self.assertEqual(result['plan']['diagnosticEvidenceDigest'], contracts.digest(diagnostic))
        self.assertEqual(result['plan']['proposalPacketDigest'], contracts.digest(self.agent.packet))
        self.assertEqual(diagnostic['recordingDigest'], self.view['recording']['recordingDigest'])
        self.assertEqual(diagnostic['sourceDigest'], self.env.source_blobs.digest)
        self.assertEqual(result['plan']['diagnosticSourceDigests'], list(dict.fromkeys(item['digest'] for item in self.references())))
        self.assertFalse(result['result']['verified'])
        self.assertNotIn(SECRET, json.dumps(diagnostic))
        self.assertNotIn('runs', json.dumps(self.repairs.get(self.fixture.principals['viewer'], result['id'])))
        from reproloop.live.access import AccessError
        with self.assertRaises(AccessError): self.repairs.diagnostics(self.fixture.principals['viewer'], result['id'])

    def test_source_only_external_approval_cannot_send_diagnostic_bytes(self):
        agent = LocalProposalDouble(); agent.external = True; agent.provider_id = 'claude'
        policy = {'schemaVersion': 1, 'projectDigest': contracts.digest(self.env.project), 'providerId': 'claude',
            'approvedTransfer': True, 'sourcePaths': [PRODUCT], 'specificationFields': ['actions']}
        self.service(agent, policy); result = self.wait(self.start()['id'])
        self.assertEqual((result['status'], result['reason']), ('blocked', 'ai_transfer_denied'))
        self.assertEqual(self.agent.calls, 0)
        self.assertEqual(result['outputs'], {})

    def test_removed_log_expires_the_derivative_even_while_the_recording_survives(self):
        self.service(); result = self.wait(self.start()['id'])
        deadline = time.monotonic() + 2
        while self.repairs._threads and time.monotonic() < deadline: time.sleep(.01)
        self.env.lab._evidence_store.tombstone(self.references()[0]['digest'], reason='operator_removed')
        self.assertIsNotNone(self.env.lab._evidence_store.lookup(self.view['recording']['recordingDigest']))
        with self.assertRaises(LiveError): self.repairs.diagnostics(self.fixture.owner, result['id'])
        self.repairs.apply_retention()
        self.assertTrue(self.repairs.journal.get(result['id'])['outputsExpired'])
        self.assertFalse((self.repairs.journal.root / result['id'] / 'diagnostics').exists())
        self.assertFalse((self.repairs.journal.root / result['id'] / 'candidate').exists())

    def test_retention_deletes_diagnostic_bytes(self):
        self.service(); result = self.wait(self.start()['id'])
        self.repairs.journal.apply_retention(now_ms=result['retainUntilMs'] + 1)
        with self.assertRaises(LiveError): self.repairs.diagnostics(self.fixture.owner, result['id'])
        self.assertFalse((self.repairs.journal.root / result['id'] / 'diagnostics').exists())

    def test_expiry_during_ai_request_cancels_and_rejects_the_late_edit(self):
        def expire(cancel):
            expiry = self.repairs.journal.list()[0]['retainUntilMs']
            with mock.patch('reproloop.live.project_repair_jobs.time.time', return_value=expiry / 1000 + 1):
                self.assertTrue(cancel.is_set())
        self.service(LocalProposalDouble(effect=expire)); result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse((self.repairs.journal.root / result['id'] / 'candidate').exists())
        self.repairs.journal.apply_retention(now_ms=result['retainUntilMs'] + 1)
        self.assertTrue(self.repairs.journal.get(result['id'])['outputsExpired'])

    def test_active_derivation_pins_its_log_source_until_provider_completion(self):
        from reproloop.live.evidence_store import EvidenceStoreError
        def remove(_):
            with self.assertRaises(EvidenceStoreError):
                self.env.lab._evidence_store.tombstone(self.references()[0]['digest'], reason='operator_removed')
        self.service(LocalProposalDouble(effect=remove)); result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'proposal-ready')

    def test_http_and_cli_export_only_the_approved_projection_without_overwriting(self):
        from reproloop.live.server import LiveServer
        agent = LocalProposalDouble(); agent.external = True; agent.provider_id = 'claude'
        policy = {'schemaVersion': 2, 'projectDigest': contracts.digest(self.env.project),
            'providerId': 'claude', 'approvedTransfer': True, 'sourcePaths': [PRODUCT],
            'specificationFields': ['actions'], 'diagnostics': {
                'policyDigest': contracts.digest(diagnostic_policy(self.env.project)),
                'eventFields': ['seq', 'type', 'name']}}
        self.service(agent, policy); result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'proposal-ready')
        server = LiveServer(self.env.lab, access=self.fixture.access, issue_workflow=self.workflow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close(): server.shutdown(); thread.join(2); server.server_close()
        self.addCleanup(close)
        token = self.fixture.access_store.issue_principal_credential('admin', 'owner', lifetime_seconds=600)['token']
        output = self.env.root / 'diagnostic-review'
        command = [sys.executable, '-m', 'reproloop', 'live-issues', 'repair-diagnostics', result['id'],
                   '--output', str(output), '--server', server.origin, '--credential-stdin']
        def cli():
            value = subprocess.run(command, input=token + '\n', text=True, capture_output=True,
                timeout=15, cwd=Path(__file__).resolve().parents[1])
            self.assertNotIn(token, value.stdout + value.stderr)
            self.assertNotIn(SECRET, value.stdout + value.stderr)
            return value
        response = cli(); self.assertEqual(response.returncode, 0, response.stderr)
        raw = (output / 'diagnostics.json').read_bytes()
        self.assertEqual(json.loads(raw), self.agent.packet['diagnostics'])
        self.assertNotIn('returned', response.stdout)
        self.assertNotIn('target', json.dumps(self.agent.packet['diagnostics']))
        self.assertNotEqual(cli().returncode, 0)
        self.assertEqual((output / 'diagnostics.json').read_bytes(), raw)

    def import_in_independent_store(self):
        from tests.test_issue_media import png
        def synthetic_decoder(body, mime):
            self.assertEqual((body, mime), (png(8, 12), 'image/png'))
            return {'width': 8, 'height': 12, 'mimeType': mime}
        self.workflow.packages.media_validator = synthetic_decoder
        exported = self.workflow.export(self.fixture.owner, self.issue_id)['package']
        with self.workflow.packages.open_archive(exported['id'], 'checkout', authorize=lambda: True) as archive:
            raw = archive.body
        receiver = workflow_support.IssueWorkflowTests('runTest')
        with mock.patch('tests.test_issue_workflow.G4Environment', RepairEnvironment), \
             mock.patch('tests.g9_support.source_fixture', return_value=(
                 self.env.project, self.env.source_blobs, self.env.original_artifacts)):
            receiver.setUp()
        self.addCleanup(receiver.tearDown)
        self.fixture, self.workflow, self.env = receiver, receiver.workflow, receiver.env
        self.workflow.packages.media_validator = synthetic_decoder
        imported = self.workflow.import_archive(self.fixture.owner, 'checkout', io.BytesIO(raw),
            size=len(raw), digest=hashlib.sha256(raw).hexdigest())
        self.issue_id = imported['issue']['id']
        # The imported recording still needs local binding and a new baseline.
        self.workflow.approve(self.fixture.owner, self.issue_id, {'specificationDigest': self.spec_digest,
            'revision': 1, 'bindImported': True})
        self.workflow.replay(self.fixture.owner, self.issue_id, {'deviceId': 'device',
            'clientId': 'imported_baseline', 'specificationDigest': self.spec_digest})
        self.fixture.wait(self.issue_id, states={'reproduced'})
        for reference in self.references():
            self.assertIsNone(self.env.lab._evidence_store.lookup(reference['digest']))
        return imported

    def test_imported_archive_keeps_diagnostic_bindings_without_using_local_source_objects(self):
        imported = self.import_in_independent_store()
        self.service(); result = self.wait(self.start()['id'])
        self.assertEqual(result['status'], 'proposal-ready', result['reason'])
        self.assertEqual(self.agent.packet['diagnostics']['recordingDigest'], self.view['recording']['recordingDigest'])
        self.workflow.packages.tombstone(imported['issue']['packageId'], 'checkout', authorize=lambda: True)
        self.repairs.apply_retention()
        self.assertTrue(self.repairs.journal.get(result['id'])['outputsExpired'])

    def test_imported_diagnostics_do_not_lock_the_archive_during_provider_requests(self):
        imported = self.import_in_independent_store()
        entered, removed = threading.Event(), threading.Event()
        def hold(cancel): entered.set(); cancel.wait(5)
        self.service(LocalProposalDouble(effect=hold)); job = self.start()
        self.assertTrue(entered.wait(2))
        errors = []
        def remove():
            try:
                self.workflow.packages.tombstone(imported['issue']['packageId'], 'checkout', authorize=lambda: True)
                removed.set()
            except Exception as error: errors.append(type(error).__name__)
        thread = threading.Thread(target=remove); thread.start()
        try:
            self.assertTrue(removed.wait(.5), 'Provider work blocked an independent archive operation')
            self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        finally:
            self.repairs.cancel(self.fixture.owner, job['id'])
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__': unittest.main()
