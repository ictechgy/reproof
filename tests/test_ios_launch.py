import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from reproof.core import ContractError
from reproof.ios_launch import (ApprovedIosLaunch, validate_ios_launch,
                                require_ios_launch, load_ios_launch)
from reproof.ios_profile import validate_ios_profile
from tests.test_worker_profiles import ios_document


def fixture():
    profile = validate_ios_profile(ios_document())
    config = {'schemaVersion': 1, 'kind': 'reproof-approved-ios-launch',
              'referenceId': profile.data['approvedReferences']['launch'],
              'profileDigest': profile.digest,
              'artifactDigest': profile.data['artifact']['sha256'],
              'environment': [
                  {'key': 'QA_SERVER', 'type': 'string', 'value': 'https://qa.example.invalid/base/'},
                  {'key': 'QA_MODE', 'type': 'string', 'value': 'qa'},
                  {'key': 'QA_FIXTURE_ID', 'type': 'string', 'value': 'checkout_7'},
                  {'key': 'QA_FIXTURE_VERSION', 'type': 'integer', 'value': 7}]}
    return profile, config


class IosLaunchTests(unittest.TestCase):
    def test_immutable_canonical_binding_and_private_repr(self):
        profile, config = fixture()
        selected = validate_ios_launch(config, profile)
        self.assertEqual(require_ios_launch(selected, profile).digest, selected.digest)
        expected = dict(config, environment=sorted(config['environment'], key=lambda x: x['key']))
        raw = json.dumps(expected, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.assertEqual(selected.native()['REPRO_LIVE_APPROVED_LAUNCH'], raw.decode())
        self.assertEqual(selected.digest, hashlib.sha256(raw).hexdigest())
        self.assertNotIn('qa.example', repr(selected))
        config['environment'][0]['value'] = 'changed'
        self.assertNotEqual(config, selected.data)
        mutable = selected.data;mutable['kind'] = 'changed'
        self.assertNotEqual(mutable, selected.data)
        changed = profile.data;changed['buildId'] = 'candidate'
        with self.assertRaises(ContractError):
            require_ios_launch(selected, validate_ios_profile(changed))
        with self.assertRaises(ContractError):
            require_ios_launch(ApprovedIosLaunch(raw + b' '), profile)

    def test_closed_keys_bindings_and_types(self):
        profile, config = fixture()
        variants = []
        for key in ('referenceId', 'profileDigest', 'artifactDigest', 'kind'):
            value = copy.deepcopy(config);value[key] = 'wrong';variants.append(value)
        for version in (True, 1.0, '1', None):
            value = copy.deepcopy(config);value['schemaVersion'] = version;variants.append(value)
        for key in ('PATH', 'DYLD_INSERT_LIBRARIES', 'REPRO_MODE', 'QA_TOKEN', 'QA_SECRET', 'QA_UNKNOWN'):
            value = copy.deepcopy(config);value['environment'][0]['key'] = key;variants.append(value)
        for setting in (True, 0, -1, 2**31, 1.0, '7'):
            value = copy.deepcopy(config);value['environment'][3]['value'] = setting;variants.append(value)
        for setting in ('production', '', 'qa\n', '$(id)', 'qa;id', '/tmp/x'):
            value = copy.deepcopy(config);value['environment'][1]['value'] = setting;variants.append(value)
        for setting in ('../file', 'a.b', 'id\n', '한글', 'x'*65, 'a b'):
            value = copy.deepcopy(config);value['environment'][2]['value'] = setting;variants.append(value)
        value = copy.deepcopy(config);value['environment'][0]['type'] = 'url';variants.append(value)
        value = copy.deepcopy(config);value['environment'][0]['secret'] = True;variants.append(value)
        value = copy.deepcopy(config);value['environment'][1] = value['environment'][0];variants.append(value)
        for items in ([], config['environment'][:-1], config['environment'][2:3]):
            value = copy.deepcopy(config);value['environment'] = items;variants.append(value)
        value = copy.deepcopy(config);value['command'] = 'true';variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_ios_launch(value, profile)

    def test_url_policy_is_bounded_and_no_secret_or_command_transport(self):
        profile, config = fixture()
        for url in ('https://qa.example', 'https://qa.example/', 'https://qa.example/a_1~z/file-v2.json'):
            config['environment'][0]['value'] = url
            validate_ios_launch(config, profile)
        for url in ('http://qa.example', 'https://QA.example', 'https://localhost', 'https://127.0.0.1',
                    'https://qa.example:443', 'https://user:pass@qa.example', 'https://qa.example?x=1',
                    'https://qa.example?', 'https://qa.example/#', 'https://qa.example/%20',
                    'https://qa.example/../x', 'https://qa.example/.', 'https://qa.example//x',
                    'https://qa.example/hello world', 'https://qa.example/$(id)',
                    'https://qa.example\\x', 'https://-qa.example', 'https://qa-.example',
                    'file:///tmp/x', 'https://qa.example/\n', 'https://' + 'a'*64 + '.example'):
            config['environment'][0]['value'] = url
            with self.subTest(url=url), self.assertRaises(ContractError):
                validate_ios_launch(config, profile)

    def test_loader_rejects_duplicates_links_oversize_and_nonfinite(self):
        profile, config = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'selection.json'
            path.write_text(json.dumps(config))
            self.assertEqual(load_ios_launch(path, profile).data, validate_ios_launch(config, profile).data)
            alias = Path(directory)/'alias.json';alias.symlink_to(path)
            with self.assertRaises(ContractError):load_ios_launch(alias, profile)
            for raw in ('{"schemaVersion":1,"schemaVersion":1}', ' ' * 8193,
                        json.dumps(config).replace('"value": 7', '"value": NaN')):
                path.write_text(raw)
                with self.assertRaises(ContractError):load_ios_launch(path, profile)
            path.unlink()
            with self.assertRaises(ContractError):load_ios_launch(path, profile)


if __name__ == '__main__':
    unittest.main()
