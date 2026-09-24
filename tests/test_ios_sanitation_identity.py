from copy import deepcopy
import unittest

from reproof.core import ContractError
from reproof.ios_runtime_identity import validate_ios_runtime_identity
from tests.test_ios_runtime_identity import BASE


class IOSSanitationIdentityTests(unittest.TestCase):
    def marker(self, stage='launch'):
        return dict(BASE, schemaVersion=2, sanitation={
            'schemaVersion':1, 'kind':'ios-app-sanitation-receipt',
            'policyDigest':'b'*64, 'runId':BASE['runId'], 'stage':stage,
            'startedAtMs':1200, 'completedAtMs':1234,
            'pathCount':2, 'userDefaultsKeyCount':1, 'keychainItemCount':0, 'status':'complete'})

    def validate(self, document, **changes):
        options=dict(bundle_id=BASE['bundleId'], build_id=BASE['buildId'],
            run_id=BASE['runId'], profile_digest=BASE['profileDigest'],
            sanitation_policy_digest='b'*64,
            sanitation_counts={'pathCount':2,'userDefaultsKeyCount':1,'keychainItemCount':0},
            sanitation_stage='launch')
        options.update(changes)
        return validate_ios_runtime_identity(document, **options)

    def test_launch_and_cleanup_are_separate_exact_policy_observations(self):
        for stage in ('launch', 'cleanup'):
            value=self.marker(stage)
            observed=self.validate(value, sanitation_stage=stage)
            self.assertEqual(observed.sanitation.public(), value['sanitation'])
            self.assertEqual(observed.sanitation.grade, 'app-self-verified-sanitation')
            self.assertEqual(observed.public()['schemaVersion'], 2)
            with self.assertRaises(ContractError):
                self.validate(value, sanitation_stage='cleanup' if stage=='launch' else 'launch')

    def test_missing_wrong_policy_run_counts_and_incomplete_receipts_fail_closed(self):
        with self.assertRaises(ContractError): self.validate(BASE)
        for key,value in (('policyDigest','c'*64),('runId','22222222-2222-4222-8222-222222222222'),
                          ('pathCount',1),('pathCount',True),('userDefaultsKeyCount',2),
                          ('keychainItemCount',1),('status','partial'),('completedAtMs',1199),
                          ('extra','unexpected')):
            with self.subTest(key=key,value=value):
                marker=self.marker();marker['sanitation'][key]=value
                with self.assertRaises(ContractError): self.validate(marker)
        marker=self.marker();marker['sanitationPolicyDigest']='b'*64
        with self.assertRaises(ContractError):self.validate(marker)

    def test_sanitation_cannot_be_silently_accepted_without_expected_contract(self):
        with self.assertRaises(ContractError):
            self.validate(self.marker(), sanitation_policy_digest=None, sanitation_counts=None)
        with self.assertRaises(ContractError):self.validate(self.marker(), sanitation_counts=None)


if __name__=='__main__':unittest.main()
