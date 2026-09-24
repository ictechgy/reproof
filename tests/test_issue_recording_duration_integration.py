"""Natural expiry through real issue, fixture, recording and cleanup services."""
import unittest
from unittest import mock

from tests import test_issue_workflow as workflow_helpers, g4_support
from tests.g4_support import ScenarioProvider
from tests.test_app_logs import snapshot


class IssueDurationIntegrationTests(unittest.TestCase):
    def setUp(self):
        original = g4_support.project_document
        def logged_project():
            project = original()
            project['evidencePolicy']['logs'] = True
            return project
        with mock.patch.object(g4_support, 'project_document', side_effect=logged_project):
            workflow_helpers.IssueWorkflowTests.setUp(self)

    tearDown = workflow_helpers.IssueWorkflowTests.tearDown
    start = workflow_helpers.IssueWorkflowTests.start
    wait = workflow_helpers.IssueWorkflowTests.wait

    def test_natural_deadline_collects_final_logs_and_releases_fixture_and_device(self):
        calls = []
        class LoggedProvider(ScenarioProvider):
            def collect_app_logs(self):
                calls.append('terminal-log-snapshot')
                return snapshot()
        self.env.lab.devices['device']['capabilities']['automaticAppLogs'] = True
        self.env.lab.devices['device']['factory'] = lambda: LoggedProvider(self.env.control)
        issue_id = self.start()
        active = self.wait(issue_id, states={'recording'})
        session = self.env.lab._session(active['issue']['sessionId'], 'owner')
        recorder = session['releaseRecorder']
        clock = recorder.anchor.synchronizer._clock
        clock.advance(600_001_000_000)
        self.assertTrue(recorder.duration_reached())
        self.assertTrue(self.workflow._recording_guards[issue_id]('observe'))
        final = self.wait(issue_id, states={'complete', 'failed', 'cancelled', 'quarantined'})
        self.assertEqual(final['issue']['state'], 'complete')
        self.assertIsNone(final['issue']['reason'])
        self.assertEqual(calls, ['terminal-log-snapshot'])
        self.assertTrue(any(item['mimeType'] == 'application/vnd.reproof.app-log+json'
                            for item in final['recording']['original']['observations']))
        self.assertEqual(recorder.store._recording_row(recorder.recording_id)['barrier_offset_ms'], 600_000)
        self.assertEqual(final['lifecycle']['deviceCleanup'], 'complete')
        self.assertTrue(all(item['status'] == 'complete' for item in final['lifecycle']['cleanup']))
        self.assertEqual(self.env.lab.list_devices()[0]['state'], 'available')


if __name__ == '__main__':
    unittest.main()
