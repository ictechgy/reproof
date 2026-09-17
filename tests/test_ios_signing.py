import copy
from datetime import datetime, timedelta
import hashlib
import unittest
from reproloop.ios_signing import eligible_profile


class LocalSigningTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10)
        self.identity = hashlib.sha1(b'test certificate').hexdigest().upper()
        self.profile = {'ExpirationDate': self.now + timedelta(days=1),
            'ProvisionedDevices': ['fixture-device'], 'TeamIdentifier': ['FIXTURE123'],
            'ApplicationIdentifierPrefix': ['FIXTURE123'], 'DeveloperCertificates': [b'test certificate'],
            'Entitlements': {'application-identifier': 'FIXTURE123.io.reproloop.*',
                            'com.apple.developer.team-identifier': 'FIXTURE123', 'get-task-allow': True}}

    def eligible(self, profile=None, device='fixture-device', bundles=None, identities=None):
        return eligible_profile(self.profile if profile is None else profile, device,
            ['io.reproloop.sample.ios'] if bundles is None else bundles,
            {self.identity} if identities is None else identities, self.now)

    def test_valid_profile_requires_matching_available_private_identity(self):
        self.assertEqual(self.eligible(), self.identity)
        self.assertIsNone(self.eligible(identities=set()))

    def test_rejects_expired_wrong_device_and_nonfixture_apps(self):
        expired = copy.deepcopy(self.profile)
        expired['ExpirationDate'] = self.now
        self.assertIsNone(self.eligible(expired))
        self.assertIsNone(self.eligible(device='unregistered'))
        self.assertIsNone(self.eligible(bundles=['com.other.app']))
        self.assertIsNone(self.eligible(bundles=[]))

    def test_rejects_distribution_and_mismatched_team_or_application(self):
        for key, value in [('get-task-allow', False), ('application-identifier', 'FIXTURE123.other.*'),
                           ('com.apple.developer.team-identifier', 'OTHER12345')]:
            profile = copy.deepcopy(self.profile)
            profile['Entitlements'][key] = value
            self.assertIsNone(self.eligible(profile))


if __name__ == '__main__':
    unittest.main()
