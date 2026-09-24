"""A reaped recording must still release its prepared fixture."""
import unittest

from reproof.live.model import LiveError
from tests import test_issue_workflow as workflow_helpers


class IssueIdleCleanupTests(unittest.TestCase):
    setUp = workflow_helpers.IssueWorkflowTests.setUp
    tearDown = workflow_helpers.IssueWorkflowTests.tearDown
    start = workflow_helpers.IssueWorkflowTests.start
    wait = workflow_helpers.IssueWorkflowTests.wait

    def test_idle_close_finishes_issue_cleanup_without_revising_original(self):
        now = [0.0]
        self.env.lab.clock = lambda: now[0]
        issue_id = self.start()
        active = self.wait(issue_id, states={'recording'})
        sid = active['issue']['sessionId']
        now[0] = self.env.lab.idle_timeout + 1
        self.env.lab.reap_expired()
        closed = self.env.lab.peek_session(sid, 'owner')
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(closed['closeReason'], 'idle_timeout')
        before = self.env.lab.release_recording(active['issue']['recordingId'], 'owner')
        self.assertEqual(before['status'], 'frozen-incomplete')
        final = self.wait(issue_id, states={'failed', 'cancelled', 'quarantined'})
        self.assertEqual(final['issue']['state'], 'failed')
        self.assertEqual(final['recording']['recordingDigest'], before['recordingDigest'])
        self.assertEqual(final['recording']['original'], before['original'])
        self.assertEqual(final['lifecycle']['deviceCleanup'], 'complete')
        self.assertTrue(all(item['status'] == 'complete' for item in final['lifecycle']['cleanup']))
        self.assertNotIn(issue_id, self.workflow._handles)
        self.assertEqual(self.env.lab.list_devices()[0]['state'], 'available')

    def test_closed_label_cannot_replace_a_frozen_recording_barrier(self):
        issue_id = self.start()
        active = self.wait(issue_id, states={'recording'})
        session = self.env.lab._session(active['issue']['sessionId'], 'owner')
        # A corrupted in-memory label cannot grant terminal evidence authority.
        with session['lock']:
            session['state'] = 'closed'
            try:
                with self.assertRaises(LiveError):
                    self.env.lab.begin_release_stop(
                        session['id'], 'owner', session['controllerId'],
                        session['epoch'], cleanup_only=True)
                self.assertIsNone(session['releaseRecorder']._row()['barrier_sequence'])
            finally:
                session['state'] = 'active'


if __name__ == '__main__':
    unittest.main()
