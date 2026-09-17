"""Normal issue stop admits an automatic snapshot before its durable barrier."""
import copy
import threading
import time
import unittest
from unittest import mock

from reproloop.app_logs import APP_LOG_MIME
from reproloop.live.model import LiveError
from tests.g4_support import ScenarioProvider
from tests.test_fixture_allocations import project_document
from tests.test_repair_diagnostics import app_log
from tests import test_issue_workflow as support


class AutomaticLogProvider(ScenarioProvider):
    def collect_app_logs(self):
        self.control['log_calls'] += 1
        self.control['log_entered'].set()
        self.control['log_continue'].wait(5)
        if self.control.get('log_error'):
            raise LiveError('app_log_unavailable', 'Synthetic collector failure')
        return app_log()


class IssueAutomaticLogTests(unittest.TestCase):
    def fixture(self, *, logs=True):
        project = project_document(); project['evidencePolicy']['logs'] = logs
        self.support = support.IssueWorkflowTests('runTest')
        with mock.patch('tests.g4_support.project_document', return_value=project):
            self.support.setUp()
        self.addCleanup(self.support.tearDown)
        self.env, self.workflow, self.owner = self.support.env, self.support.workflow, self.support.owner
        self.env.control.update(log_calls=0, log_entered=threading.Event(), log_continue=threading.Event())
        self.env.control['log_continue'].set()
        self.env.lab.devices['device']['factory'] = lambda: AutomaticLogProvider(self.env.control)
        self.env.lab.devices['device']['capabilities']['automaticAppLogs'] = True

    def test_normal_issue_stop_collects_logs_without_opening_the_log_panel(self):
        self.fixture()
        issue_id, result = self.support.recorded()
        self.assertEqual(result['issue']['state'], 'complete')
        references = result['recording']['original']['observations']
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['mimeType'], APP_LOG_MIME)
        self.assertEqual(self.env.control['log_calls'], 1)
        sid = result['issue']['sessionId']
        self.assertEqual(self.env.lab.app_logs(sid, 'owner'), app_log())
        original = copy.deepcopy(result['recording']['original'])
        self.workflow.stop(self.owner, issue_id)
        self.assertEqual(self.env.control['log_calls'], 1)
        self.assertEqual(self.workflow.get(self.owner, issue_id)['recording']['original'], original)

    def test_disabled_log_policy_has_no_collector_effect(self):
        self.fixture(logs=False)
        _, result = self.support.recorded()
        self.assertEqual(result['issue']['state'], 'complete')
        self.assertEqual(self.env.control['log_calls'], 0)
        self.assertEqual(result['recording']['original']['observations'], [])

    def test_cancel_does_not_collect_additional_data(self):
        self.fixture()
        issue_id = self.support.start(); self.support.wait(issue_id, states={'recording'})
        self.workflow.stop(self.owner, issue_id, cancel=True)
        result = self.support.wait(issue_id, states={'cancelled'})
        self.assertEqual(self.env.control['log_calls'], 0)
        self.assertEqual(result['recording']['original']['observations'], [])
        self.assertEqual(self.env.lab.list_devices()[0]['state'], 'available')

    def test_snapshot_failure_preserves_inputs_and_does_not_prevent_cleanup(self):
        self.fixture(); self.env.control['log_error'] = True
        _, result = self.support.recorded()
        self.assertEqual(self.env.control['log_calls'], 1)
        self.assertEqual(result['recording']['status'], 'frozen-incomplete')
        self.assertEqual(len(result['recording']['original']['events']), 2)
        self.assertEqual(result['lifecycle']['deviceCleanup'], 'complete')
        self.assertEqual(self.env.lab.list_devices()[0]['state'], 'available')

    def test_recording_gap_failure_still_stops_the_provider(self):
        self.fixture(); self.env.control['log_error'] = True
        issue_id = self.support.start()
        active = self.support.wait(issue_id, states={'recording'})
        recorder = self.env.lab._session(active['issue']['sessionId'])['releaseRecorder']
        with mock.patch.object(recorder, 'declare_gap', side_effect=OSError('Synthetic journal failure')):
            self.workflow.stop(self.owner, issue_id)
        result = self.support.wait(issue_id, states={'failed', 'quarantined'})
        self.assertEqual(result['lifecycle']['deviceCleanup'], 'complete')
        self.assertEqual(result['recording']['status'], 'frozen-incomplete')

    def test_revocation_before_snapshot_has_no_collector_effect(self):
        self.fixture()
        issue_id = self.support.start(); self.support.wait(issue_id, states={'recording'})
        self.support.access_store.revoke_membership('admin', 'checkout', 'owner', 'operator')
        self.workflow.stop(self.owner, issue_id)
        result = self.support.wait(issue_id, states={'failed', 'cancelled', 'quarantined'})
        self.assertEqual(self.env.control['log_calls'], 0)
        self.assertEqual(result['recording']['original']['observations'], [])

    def test_revocation_during_snapshot_prevents_late_data_publication(self):
        self.fixture(); self.env.control['log_continue'].clear()
        issue_id = self.support.start(); active = self.support.wait(issue_id, states={'recording'})
        errors = []
        def stop():
            try: self.workflow.stop(self.owner, issue_id)
            except Exception as error: errors.append(type(error).__name__)
        thread = threading.Thread(target=stop); thread.start()
        try:
            self.assertTrue(self.env.control['log_entered'].wait(1))
            self.support.access_store.revoke_membership('admin', 'checkout', 'owner', 'operator')
        finally:
            self.env.control['log_continue'].set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        result = self.support.wait(issue_id, states={'failed', 'cancelled', 'quarantined'})
        self.assertEqual(result['recording']['original']['observations'], [])
        self.assertFalse('appLog' in self.env.lab._session(active['issue']['sessionId']))

    def test_pending_snapshot_does_not_hold_the_workflow_lock_or_duplicate_stop(self):
        self.fixture(); self.env.control['log_continue'].clear()
        issue_id = self.support.start(); self.support.wait(issue_id, states={'recording'})
        errors = []
        def stop():
            try: self.workflow.stop(self.owner, issue_id)
            except Exception as error: errors.append(type(error).__name__)
        thread = threading.Thread(target=stop); thread.start()
        try:
            self.assertTrue(self.env.control['log_entered'].wait(1))
            before = time.monotonic()
            self.workflow.get(self.owner, issue_id)
            self.assertLess(time.monotonic() - before, .5)
            with self.assertRaises(LiveError) as caught: self.workflow.stop(self.owner, issue_id)
            self.assertEqual(caught.exception.code, 'issue_busy')
        finally:
            self.env.control['log_continue'].set(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        result = self.support.wait(issue_id, states={'complete', 'failed', 'quarantined'})
        self.assertEqual(result['issue']['state'], 'complete')
        self.assertEqual(self.env.control['log_calls'], 1)


if __name__ == '__main__': unittest.main()
