import threading
import time
import unittest
from unittest.mock import Mock

from reproloop.live.issue_workflow import IssueWorkflow, RecordingDurationReached
from reproloop.live.model import LiveError


class IssueRecordingDurationTests(unittest.TestCase):
    def test_effect_guard_uses_bound_recording_anchor_after_preparation(self):
        workflow = object.__new__(IssueWorkflow)
        workflow._runtime = Mock(return_value=Mock(registration=Mock(project_digest='project-digest')))
        workflow.access = Mock()
        workflow.access.effect_authorizer.return_value = lambda _kind: True
        workflow._guard = Mock(return_value=lambda: True)
        cancel = threading.Event()
        document = {'projectId': 'project', 'deviceId': 'device'}
        guard = workflow._effect_guard(Mock(principal_id='owner'), document, cancel, seconds=600, recording=True)

        # Preparation and startup occur before a recorder is bound, so the
        # natural recording deadline cannot be consumed by that work.
        self.assertTrue(guard('fixture_prepare'))

        reached = [False, True]
        recorder = Mock()
        recorder.duration_reached.side_effect = lambda: reached.pop(0)
        guard.bind_recording(recorder)
        self.assertTrue(guard('session_check'))
        with self.assertRaises(RecordingDurationReached):
            guard('session_check')
        # The same callback also gates provider observations and final logs.
        # Natural expiry must not be rewritten as authorization_revoked there.
        from reproloop.live.model import Lab
        recorder.duration_reached.side_effect = None
        recorder.duration_reached.return_value = True
        for kind in ('observe', 'control', 'app_logs', 'session'):
            Lab._authorize_effect({'effectAuthorizer': guard}, kind)

    def test_cancellation_remains_distinct_from_natural_duration(self):
        workflow = object.__new__(IssueWorkflow)
        workflow._runtime = Mock(return_value=Mock(registration=Mock(project_digest='project-digest')))
        workflow.access = Mock()
        workflow.access.effect_authorizer.return_value = lambda _kind: True
        workflow._guard = Mock(return_value=lambda: True)
        cancel = threading.Event()
        document = {'projectId': 'project', 'deviceId': 'device'}
        guard = workflow._effect_guard(Mock(principal_id='owner'), document, cancel, seconds=600, recording=True)
        recorder = Mock()
        recorder.duration_reached.return_value = True
        guard.bind_recording(recorder)
        cancel.set()
        with self.assertRaisesRegex(Exception, 'Issue operation was cancelled'):
            guard('session_check')

    def test_replay_guard_keeps_its_bounded_overall_deadline(self):
        workflow = object.__new__(IssueWorkflow)
        workflow._runtime = Mock(return_value=Mock(registration=Mock(project_digest='project-digest')))
        workflow.access = Mock()
        workflow.access.effect_authorizer.return_value = lambda _kind: True
        workflow._guard = Mock(return_value=lambda: True)
        guard = workflow._effect_guard(
            Mock(principal_id='owner'), {'projectId': 'project', 'deviceId': 'device'},
            threading.Event(), seconds=0.01, recording=False)
        time.sleep(0.02)
        with self.assertRaises(LiveError) as raised:
            guard('replay')
        self.assertEqual(raised.exception.code, 'issue_timeout')

    def test_monitor_natural_stop_is_not_marked_cancelled(self):
        workflow = object.__new__(IssueWorkflow)
        workflow._monitor_stop = Mock()
        workflow._monitor_stop.wait.side_effect = [False, True]
        workflow._lock = threading.RLock()
        workflow._recording_guards = {'issue-1': lambda _kind: (_ for _ in ()).throw(RecordingDurationReached())}
        workflow._handles = {'issue-1': (Mock(), None)}
        workflow._forced_finalizations = set()
        workflow._natural_finalizations = set()
        workflow._finalizing = set()
        workflow._stop_snapshots = set()
        workflow.runtimes = {'project': Mock()}
        workflow._get = Mock(return_value={'state': 'recording', 'projectId': 'project'})
        workflow._put = Mock()
        order = []
        workflow._final_log_snapshot = lambda _runtime, _document: order.append('logs')
        workflow.runtimes['project'].service.begin_stop = Mock()
        workflow.runtimes['project'].service.begin_stop.side_effect = lambda _handle: order.append('barrier')
        finished = []
        workflow._finish = lambda _runtime, _document, *, cancelled: finished.append(cancelled)
        workflow._submit = lambda issue_id, task, before=None: (before() if before else None) or task()
        workflow._monitor()
        self.assertEqual(finished, [False])
        self.assertEqual(workflow._forced_finalizations, set())
        self.assertEqual(order, ['logs', 'barrier'])

    def test_monitor_authorization_failure_remains_cancelled(self):
        workflow = object.__new__(IssueWorkflow)
        workflow._monitor_stop = Mock()
        workflow._monitor_stop.wait.side_effect = [False, True]
        workflow._lock = threading.RLock()
        workflow._recording_guards = {'issue-1': lambda _kind: (_ for _ in ()).throw(
            LiveError('authorization_revoked', 'revoked'))}
        workflow._handles = {'issue-1': (Mock(), None)}
        workflow._forced_finalizations = set()
        workflow._natural_finalizations = set()
        workflow._finalizing = set()
        workflow.runtimes = {'project': Mock()}
        workflow._get = Mock(return_value={'state': 'recording', 'projectId': 'project'})
        workflow._put = Mock()
        workflow.runtimes['project'].service.begin_stop = Mock()
        finished = []
        workflow._finish = lambda _runtime, _document, *, cancelled: finished.append(cancelled)
        workflow._submit = lambda issue_id, task, before=None: (before() if before else None) or task()
        workflow._monitor()
        self.assertEqual(finished, [True])
        self.assertEqual(workflow._natural_finalizations, set())


if __name__ == '__main__':
    unittest.main()
