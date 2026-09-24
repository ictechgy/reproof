"""Server-side preparation authorization and immediate recording stop."""
import unittest
from unittest import mock

from reproof.live.issue_sessions import IssueSessionError
from reproof.live.model import LiveError
from reproof.live.disk_budget import DiskBudgetError
from tests.g4_support import G4Environment


class G7IssueControlTests(unittest.TestCase):
    def setUp(self): self.env = G4Environment()
    def tearDown(self): self.env.close()

    def start(self, **kwargs):
        return self.env.service.start_prepared_recording(
            device_id="device", owner="owner", controller_id="browser",
            registration=self.env.registration, application_id="ios_app", build_id="original",
            preparations=self.env.preparations(), **kwargs)

    def test_admission_closes_before_asynchronous_finalization(self):
        handle = self.start()
        current = self.env.lab.get_session(handle.session_id, "owner")
        frame = self.env.lab.frame(handle.session_id, "owner")
        result = self.env.service.begin_stop(handle)
        self.assertEqual(result["state"], "finalizing")
        with self.assertRaises((IssueSessionError, LiveError)):
            self.env.service.input(handle, {"controllerId": current["controllerId"],
                "epoch": current["epoch"], "sequence": 1, "commandId": "late_input",
                "frameId": frame["id"], "geometryVersion": frame["geometryVersion"],
                "action": "tap", "payload": {"x": .5, "y": .5}},
                recording_input={"action": "tap", "target": {
                    "kind": "accessibility-id", "value": "checkout"}, "parameters": {}})
        self.assertEqual(self.env.control["calls"], [])
        final = self.env.service.stop(handle)
        self.assertEqual(final["issue"]["deviceCleanup"], "complete")
        self.assertEqual(final["recording"]["original"]["endSequence"], 0)

    def test_authorization_denial_before_reservation_has_no_device_or_fixture_effect(self):
        def deny(_): return False
        with self.assertRaises(IssueSessionError):
            self.start(effect_authorizer=deny)
        self.assertEqual(self.env.lab.list_devices()[0]["state"], "available")
        self.assertEqual(self.env.control["calls"], [])

    def test_auth_revocation_after_preparation_denies_check_and_device_start(self):
        effects = []
        def guard(kind):
            effects.append(kind)
            return kind != "fixture_check"
        with self.assertRaises(IssueSessionError):
            self.start(effect_authorizer=guard)
        self.assertIn("fixture_prepare", effects)
        self.assertIn("fixture_check", effects)
        self.assertNotIn("provider", self.env.control)
        self.assertEqual(self.env.lab.list_devices()[0]["state"], "available")

    def test_revocation_during_recording_still_allows_owned_stop_and_cleanup(self):
        allowed = True
        def guard(_): return allowed
        handle = self.start(effect_authorizer=guard)
        allowed = False
        stopped = self.env.service.stop(handle)
        self.assertEqual(stopped["issue"]["deviceCleanup"], "complete")
        self.assertEqual(self.env.lab.list_devices()[0]["state"], "available")

    def test_storage_denial_before_provider_construction_cleans_prepared_fixtures(self):
        with mock.patch.object(self.env.lab._recording_store,'begin_recording',
                               side_effect=DiskBudgetError('Storage capacity exhausted')):
            with self.assertRaises(IssueSessionError) as caught:
                self.start()
        self.assertNotIn('provider',self.env.control)
        self.assertEqual(self.env.lab.list_devices()[0]['state'],'available')
        issue=self.env.service.get(caught.exception.issue_id)
        self.assertEqual(issue['deviceCleanup'],'complete')
        self.assertTrue(all(item['status']=='complete' for item in issue['cleanup']))

    def test_closing_old_rejected_startup_preserves_new_device_owner(self):
        def rejected_factory():
            raise RuntimeError('Synthetic provider construction failure')
        with mock.patch.dict(self.env.lab.devices['device'], factory=rejected_factory):
            with self.assertRaises(RuntimeError):
                self.env.lab.create_release_session(
                    'device', 'owner', 'browser', self.env.registration,
                    application_id='ios_app', build_id='original', preparation_receipts=[])
        old = self.env.lab.list_sessions('owner')[0]
        current = self.env.lab.create_release_session(
            'device', 'next_owner', 'next_browser', self.env.registration,
            application_id='ios_app', build_id='original', preparation_receipts=[])
        self.assertEqual(current['state'], 'active')
        self.env.lab.close_session(old['id'], 'owner')
        self.assertEqual(self.env.lab.get_session(current['id'], 'next_owner')['state'], 'active')
        device = self.env.lab.list_devices()[0]
        self.assertEqual(device['state'], 'busy')
        self.assertEqual(device['sessionId'], current['id'])
        self.assertFalse(self.env.control['provider'].closed)


if __name__ == "__main__": unittest.main()
