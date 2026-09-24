"""Repair accepts an exact trusted completed original campaign, never uploaded results."""
import copy
import unittest

from reproof import contracts
from reproof.qualification import QualificationError
from tests.g9_support import RepairEnvironment


class RepairBaselineTests(unittest.TestCase):
    def setUp(self):
        self.env = RepairEnvironment(); self.addCleanup(self.env.close)

    def test_three_actual_original_attempts_can_freeze_a_repair_baseline(self):
        result = self.env.baseline()
        trusted = self.env.engine.require_reproduced(self.env.approved, result['campaignId'])
        self.assertEqual(trusted['verdict'], 'reproduced')
        self.assertEqual(len(trusted['attempts']), 3)
        trusted['attempts'].clear()
        self.assertEqual(len(self.env.engine.require_reproduced(self.env.approved, result['campaignId'])['attempts']), 3)

    def test_absent_failed_and_uploaded_campaigns_are_rejected(self):
        with self.assertRaises(QualificationError):
            self.env.engine.require_reproduced(self.env.approved, 'missing')
        self.env.observations._adapters['screen'].value = 'success'
        result = self.env.baseline()
        with self.assertRaises(QualificationError):
            self.env.engine.require_reproduced(self.env.approved, result['campaignId'])
        with self.assertRaises(QualificationError):
            self.env.engine.require_reproduced(self.env.approved, dict(result, verdict='reproduced'))

    def test_reproduced_result_cannot_be_rebound_to_a_changed_assertion(self):
        result = self.env.baseline()
        altered = copy.deepcopy(self.env.approved)
        altered.specification['assertions'][0]['predicate']['value'] = 'another defect'
        with self.assertRaises(QualificationError):
            self.env.engine.require_reproduced(altered, result['campaignId'])

    def test_concurrent_campaign_handles_and_lookups_do_not_lose_the_campaign(self):
        from concurrent.futures import ThreadPoolExecutor
        campaign = self.env.engine.begin_original(self.env.approved, campaign_id='concurrent_campaign')
        def read(number):
            for _ in range(100):
                value = (self.env.engine.get(campaign) if number % 2
                         else self.env.engine.lookup(campaign.campaign_id))
                self.assertEqual(value['campaignId'], campaign.campaign_id)
        with ThreadPoolExecutor(max_workers=6) as workers:
            list(workers.map(read, range(6)))


if __name__ == '__main__': unittest.main()
