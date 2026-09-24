"""Repeated original replay waits for a fresh inventory report after cleanup."""
import threading
import time
import unittest
from unittest.mock import patch

from reproof.live.model import LiveError
from tests import test_issue_workflow as support


class ReplayInventoryWaitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.IssueWorkflowTests(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)

    def test_second_attempt_waits_for_inventory_without_mutating_fixtures(self):
        f = self.fixture
        issue, original = f.recorded()
        saved = f.save(issue, original['recording'])
        f.approve(issue, saved)
        pending = threading.Event()
        ready = threading.Event(); ready.set()
        class Inventory:
            def require_available(self, binding):
                if not ready.is_set():
                    pending.set()
                    raise LiveError('inventory_unavailable', 'Controlled pending ownership report', 409)
                return 'owned-inventory'
        f.env.lab.remote_inventory = Inventory()
        f.env.lab.devices['device']['_inventoryBinding'] = {'ownedTest': True}
        actual = f.env.service.replay
        completed = []
        def replay(*args, **kwargs):
            result = actual(*args, **kwargs)
            completed.append(result)
            if len(completed) == 1:
                ready.clear()
            return result
        with patch.object(f.env.service, 'replay', side_effect=replay):
            f.workflow.replay(f.owner, issue, {'deviceId': 'device', 'clientId': 'replayer',
                'specificationDigest': saved['specificationDigest']})
            self.assertTrue(pending.wait(5))
            calls = len(f.env.control['calls'])
            self.assertEqual(len(completed), 1)
            ready.set()
            result = f.wait(issue, states={'reproduced', 'failed', 'quarantined', 'cancelled'})
        self.assertEqual(result['issue']['state'], 'reproduced')
        self.assertEqual(len(completed), 3)
        self.assertEqual(len(result['campaign']['attempts']), 3)
        self.assertGreater(len(f.env.control['calls']), calls)

    def test_pending_wait_is_bounded_cancelled_and_does_not_bypass_quarantine(self):
        f = self.fixture
        f.env.lab.devices['device']['_inventoryBinding'] = {'ownedTest': True}
        class Inventory:
            def require_available(self, binding):
                raise LiveError('inventory_unavailable', 'Controlled unavailable inventory', 409)
        f.env.lab.remote_inventory = Inventory()
        before = list(f.env.control['calls'])
        with self.assertRaises(LiveError):
            f.workflow._wait_replay_inventory('device', threading.Event(), lambda _: True, timeout_seconds=.03)
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(LiveError) as caught:
            f.workflow._wait_replay_inventory('device', cancelled, lambda _: True, timeout_seconds=5)
        self.assertEqual(caught.exception.code, 'cancelled')
        f.env.lab.devices['device']['state'] = 'quarantined'
        started = time.monotonic()
        with self.assertRaises(LiveError):
            f.workflow._wait_replay_inventory('device', threading.Event(), lambda _: True, timeout_seconds=5)
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual(f.env.control['calls'], before)
        f.env.lab.devices['device']['state'] = 'available'


if __name__ == '__main__':
    unittest.main()
