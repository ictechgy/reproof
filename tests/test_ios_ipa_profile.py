import unittest
from reproof.core import ContractError
from reproof.ios_profile import validate_ios_profile
from tests.test_worker_profiles import physical_ios_document


class IOSIPAProfileTests(unittest.TestCase):
    def test_explicit_ipa_kind_binds_container_identity_without_changing_app_profiles(self):
        original=physical_ios_document()
        app=validate_ios_profile(original)
        selected=physical_ios_document('4'*64)
        selected['artifact'].update(kind='ios-ipa',bytes=1024)
        ipa=validate_ios_profile(selected)
        self.assertEqual(ipa.application_identity['artifactDigest'],'4'*64)
        self.assertEqual(ipa.data['artifact']['kind'],'ios-ipa')
        self.assertEqual(app.data,original)
        self.assertNotEqual(ipa.digest,app.digest)

    def test_ipa_profile_rejects_oversized_container(self):
        selected=physical_ios_document()
        selected['artifact'].update(kind='ios-ipa',bytes=64*1024*1024+1)
        with self.assertRaises(ContractError):validate_ios_profile(selected)


if __name__=='__main__':unittest.main()
