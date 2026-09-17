import copy
from pathlib import Path
import tempfile
import unittest

from reproloop.android_profile import validate_app_profile
from reproloop.core import ContractError
from reproloop.storage import create_bundle, load_bundle
from tests.test_android_profile import profile_document


def auto_profile_document():
    document = profile_document()
    document['captureMode'] = 'debug_receiver'
    document['targets']['report'] = None
    document['instrumentation'] = {'kind': 'kotlin_psi_v1', 'sites': [
        {'id': 's123abc', 'path': 'app/src/main/java/example/MainActivity.kt',
         'line': 12, 'target': 'commit', 'kind': 'tap'}]}
    return document


def captured(profile):
    return {'schemaVersion': 1, 'sessionId': 'auto-session', 'fixture': profile.data['fixture'],
        'startState': profile.data['startState'], 'truncated': False, 'lostEvents': False, 'endSequence': 2,
        'events': [{'id': 'e1', 'seq': 1, 'action': 'replace', 'target': 'label', 'parameters': {'value': 'QA'}},
                   {'id': 'e2', 'seq': 2, 'action': 'tap', 'target': 'commit', 'parameters': {}}]}


def diagnostics(profile):
    return {'schemaVersion': 1, 'sessionId': 'auto-session', 'appProfileDigest': profile.digest,
        'endSequence': 2, 'actions': [{'eventId': 'e2', 'target': 'commit', 'siteId': 's123abc',
            'before': {'quantity': '0'}, 'after': {'quantity': '2'}, 'outcome': 'returned'}]}


class InstrumentationContractTests(unittest.TestCase):
    def test_debug_receiver_requires_bound_source_sites(self):
        profile = validate_app_profile(auto_profile_document())
        self.assertEqual(profile.data['captureMode'], 'debug_receiver')
        self.assertIsNone(profile.native()['targets']['report'])
        for mutate in [lambda d: d.pop('instrumentation'),
                       lambda d: d['instrumentation']['sites'][0].update(path='../Other.kt'),
                       lambda d: d['instrumentation']['sites'][0].update(target='unknown'),
                       lambda d: d['instrumentation']['sites'].append(d['instrumentation']['sites'][0].copy())]:
            document = auto_profile_document(); mutate(document)
            with self.assertRaises(ContractError):
                validate_app_profile(document)

    def test_diagnostics_bind_sdk_session_events_profile_and_source_sites(self):
        from reproloop.instrumentation_diagnostics import validate_diagnostics
        profile = validate_app_profile(auto_profile_document())
        result = validate_diagnostics(diagnostics(profile), captured(profile), profile)
        self.assertEqual(result['actions'][0]['after'], {'quantity': '2'})
        for mutate in [lambda d: d.update(sessionId='stale-session'),
                       lambda d: d.update(appProfileDigest='0' * 64),
                       lambda d: d.update(endSequence=1),
                       lambda d: d['actions'][0].update(eventId='e1'),
                       lambda d: d['actions'][0].update(siteId='unknown'),
                       lambda d: d['actions'][0].update(message='private exception message'),
                       lambda d: d['actions'][0].update(outcome=[]),
                       lambda d: d['actions'][0]['after'].update(quantity='not-allowed'),
                       lambda d: d.update(actions=[])]:
            value = diagnostics(profile); mutate(value)
            with self.assertRaises(ContractError):
                validate_diagnostics(value, captured(profile), profile)

    def test_auto_capture_bundle_requires_and_protects_diagnostics(self):
        profile = validate_app_profile(auto_profile_document())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); apk = root / 'app.apk'; apk.write_bytes(b'fixture')
            with self.assertRaises(ContractError):
                create_bundle(captured(profile), profile.oracle(), apk, root / 'missing', app_profile=profile)
            self.assertFalse((root / 'missing').exists())
            bundle = create_bundle(captured(profile), profile.oracle(), apk, root / 'valid',
                app_profile=profile, diagnostics=diagnostics(profile))
            self.assertIn('diagnostics.json', bundle['manifest']['files'])
            self.assertEqual(bundle['scenario']['diagnostics']['actions'][0]['eventId'], 'e2')
            (root / 'valid/diagnostics.json').write_text('{}')
            with self.assertRaises(ContractError):
                load_bundle(root / 'valid', app_profile=profile)


if __name__ == '__main__':
    unittest.main()
